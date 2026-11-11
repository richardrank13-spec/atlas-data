"""Offline joint BTC/ETH probabilistic forecasting. No execution or network code."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import math

import numpy as np
import pandas as pd

ASSETS = ("BTC", "ETH")
MODELS = ("gaussian", "historical", "conditional")
HORIZONS = (4, 24)
LEVELS = (0.50, 0.80, 0.95)
VERSION = "liqzo-scenario-lab-0.1"
DATA_REF = "7190a803451e93e4e42aa7eac05bca48ac27f866"


def iso(ts: int) -> str:
    return pd.Timestamp(ts, unit="s", tz="UTC").isoformat().replace("+00:00", "Z")


def check_frame(df: pd.DataFrame, label: str) -> None:
    required = ["open_time", "open", "high", "low", "close", "volume", "taker_buy_volume"]
    if not set(required).issubset(df.columns) or df.empty:
        raise ValueError(f"{label}: empty or missing columns")
    if not np.isfinite(df[required].to_numpy(float)).all():
        raise ValueError(f"{label}: nonfinite input")
    t = df.open_time.to_numpy()
    if np.any(t % 3600 != 0) or np.any(np.diff(t) <= 0):
        raise ValueError(f"{label}: timestamps must be unique, ascending UTC hours")
    o, h, l, c = (df[k].to_numpy() for k in ["open", "high", "low", "close"])
    if np.any(l <= 0) or np.any(h < np.maximum(o, c)) or np.any(l > np.minimum(o, c)):
        raise ValueError(f"{label}: invalid OHLC")
    v, tb = df.volume.to_numpy(), df.taker_buy_volume.to_numpy()
    if np.any(v < 0) or np.any(tb < 0) or np.any(tb > v + 1e-7):
        raise ValueError(f"{label}: invalid volume")


@dataclass
class MarketData:
    time: np.ndarray             # bar-close timestamps, not open timestamps
    close: np.ndarray            # [time, asset], missing prices stay NaN
    returns: np.ndarray          # hourly log returns; no bridge across a gap
    sigma: np.ndarray            # trailing 24-hour RMS
    slow_sigma: np.ndarray       # trailing 168-hour RMS
    features: np.ndarray         # 6 features available at this close only
    quality: dict

    def index(self, timestamp: int) -> int:
        i = int(np.searchsorted(self.time, timestamp))
        if i >= len(self.time) or self.time[i] != timestamp:
            raise ValueError("origin must be an exact observed-grid bar close")
        return i


def from_frames(frames: dict[str, pd.DataFrame]) -> MarketData:
    for name, frame in frames.items():
        check_frame(frame, name)
    start = min(int(f.open_time.iloc[0]) for f in frames.values())
    end = max(int(f.open_time.iloc[-1]) for f in frames.values())
    grid = np.arange(start, end + 1, 3600, dtype=np.int64)
    columns, volume, taker, gaps = [], [], [], {}
    for asset in ASSETS:
        df = frames[asset]
        aligned = df.set_index("open_time").reindex(grid)
        columns.append(aligned.close.to_numpy())
        volume.append(aligned.volume.to_numpy())
        taker.append(aligned.taker_buy_volume.to_numpy())
        bad = np.flatnonzero(aligned.close.isna().to_numpy())
        segments = np.split(bad, np.flatnonzero(np.diff(bad) != 1) + 1) if len(bad) else []
        gaps[asset] = [{"first_missing_open": iso(grid[s[0]]),
                        "last_missing_open": iso(grid[s[-1]]), "hours": len(s)}
                       for s in segments]
    close = np.stack(columns, axis=1)
    r = np.full_like(close, np.nan)
    r[1:] = np.diff(np.log(close), axis=0)
    rf = pd.DataFrame(r)
    sigma = np.sqrt(rf.pow(2).rolling(24, min_periods=24).mean()).to_numpy()
    slow = np.sqrt(rf.pow(2).rolling(168, min_periods=168).mean()).to_numpy()
    trend = rf.rolling(24, min_periods=24).sum().to_numpy() / (slow * np.sqrt(24))
    ratio = np.log(sigma / slow)
    vol = pd.DataFrame(np.stack(volume, axis=1)).rolling(24, min_periods=24).sum()
    tb = pd.DataFrame(np.stack(taker, axis=1)).rolling(24, min_periods=24).sum()
    imbalance = ((2 * tb - vol) / vol.replace(0, np.nan)).to_numpy()
    features = np.concatenate([trend, ratio, imbalance], axis=1)
    quality = {"bar_open_start": iso(start), "latest_bar_close": iso(end + 3600),
               "grid_rows": len(grid), "missing": gaps,
               "source_rows": {a: len(f) for a, f in frames.items()},
               "timing": "bar open + 3600 seconds; event-time replay, publication latency unknown",
               "data_commit": DATA_REF}
    return MarketData(grid + 3600, close, r, sigma, slow, features, quality)


def load_data(directory: Path) -> MarketData:
    frames, files = {}, []
    for asset in ASSETS:
        paths = sorted(directory.glob(f"um_{asset.lower()}usdt_1h_*.csv"))
        if not paths:
            raise ValueError(f"no files for {asset}")
        for p in paths:
            files.append({"path": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                          "bytes": p.stat().st_size})
        frames[asset] = pd.concat([pd.read_csv(p) for p in paths], ignore_index=True)
    data = from_frames(frames)
    data.quality["files"] = files
    return data


class ScenarioEngine:
    """Historical analog paths with a common BTC/ETH origin; fixed causal features."""
    def __init__(self, data: MarketData, window_hours: int = 365 * 24,
                 neighbors: int = 200, uniform_floor: float = 0.25,
                 min_history: int = 1000):
        if not 0 < uniform_floor <= 1 or neighbors < 1:
            raise ValueError("invalid weighting configuration")
        self.data, self.window_hours = data, window_hours
        self.neighbors, self.uniform_floor = neighbors, uniform_floor
        self.min_history = min_history

    def fit(self, origin: int) -> dict:
        d = self.data
        if origin < 168 or origin >= len(d.time):
            raise ValueError("insufficient origin history")
        if not np.isfinite(d.features[origin]).all() or not (d.sigma[origin] > 0).all():
            raise ValueError("origin lacks 168 contiguous valid hourly returns")
        # Every candidate's whole 24h target must have matured by origin.
        starts = np.arange(max(168, origin - self.window_hours), origin - 24 + 1)
        if not len(starts):
            raise ValueError("no matured historical paths")
        valid = np.isfinite(d.features[starts]).all(axis=1) & (d.sigma[starts] > 0).all(axis=1)
        starts = starts[valid]
        future = d.returns[starts[:, None] + np.arange(1, 25)[None, :]]
        valid = np.isfinite(future).all(axis=(1, 2))
        starts, future = starts[valid], future[valid]
        if len(starts) < self.min_history:
            raise ValueError(f"only {len(starts)} eligible paths")
        scale = d.features[starts].std(axis=0)
        scale = np.maximum(scale, 1e-8)
        distance = np.mean(((d.features[starts] - d.features[origin]) / scale) ** 2, axis=1)
        kth = min(self.neighbors, len(starts)) - 1
        bandwidth2 = max(float(np.partition(distance, kth)[kth]), 1e-8)
        kernel = np.exp(-0.5 * distance / bandwidth2)
        kernel /= kernel.sum()
        weights = (1 - self.uniform_floor) * kernel + self.uniform_floor / len(starts)
        normalized_paths = np.cumsum(future, axis=1) / d.sigma[starts, None, :]
        past = d.returns[origin - 167:origin + 1]
        rho = float(np.corrcoef(past.T)[0, 1])
        rho = float(np.clip(rho if np.isfinite(rho) else 0.0, -0.999, 0.999))
        return {"origin": origin, "starts": starts, "paths": normalized_paths,
                "weights": weights, "sigma": d.sigma[origin].copy(), "rho": rho,
                "diagnostics": {"eligible_historical_paths": len(starts),
                    "weight_effective_support": float(1 / np.sum(weights ** 2)),
                    "independent_historical_samples": None,
                    "support_warning": "24h windows overlap; effective weight support is NOT independent sample size",
                    "latest_library_target_close": iso(d.time[int(starts.max()) + 24]),
                    "first_library_origin": iso(d.time[int(starts.min())]),
                    "uniform_tail_weight": self.uniform_floor,
                    "feature_values": d.features[origin].tolist(), "rho_168h": rho}}

    @staticmethod
    def simulate(fit: dict, model: str, count: int, seed: int) -> np.ndarray:
        if model not in MODELS or count < 1:
            raise ValueError("unknown model or invalid simulation count")
        rng = np.random.default_rng(seed)
        if model == "gaussian":
            z = rng.standard_normal((count, 24, 2))
            z[:, :, 1] = fit["rho"] * z[:, :, 0] + math.sqrt(1 - fit["rho"] ** 2) * z[:, :, 1]
            return np.cumsum(z * fit["sigma"], axis=1)
        weights = fit["weights"] if model == "conditional" else np.full(len(fit["starts"]), 1 / len(fit["starts"]))
        # Common uniforms allow paired candidate / baseline comparisons.
        ix = np.searchsorted(np.cumsum(weights), rng.random(count), side="right")
        ix = np.minimum(ix, len(weights) - 1)
        return fit["paths"][ix] * fit["sigma"]


def crps(samples: np.ndarray, observed: float) -> float:
    """Exact CRPS of the empirical distribution; O(n log n)."""
    x = np.sort(np.asarray(samples, float))
    n = len(x)
    pair_half = np.sum((2 * np.arange(1, n + 1) - n - 1) * x) / n ** 2
    return float(np.mean(np.abs(x - observed)) - pair_half)


def interval_score(lower: float, upper: float, y: float, coverage: float) -> float:
    alpha = 1 - coverage
    return float(upper - lower + 2 / alpha * max(lower - y, 0) + 2 / alpha * max(y - upper, 0))


def wis(median: float, intervals: dict, y: float) -> float:
    result = 0.5 * abs(y - median)
    for coverage in LEVELS:
        lower, upper = intervals[coverage]
        result += (1 - coverage) / 2 * interval_score(lower, upper, y, coverage)
    return result / (len(LEVELS) + 0.5)


class IntervalCalibrator:
    """Past forecast errors only; no conditional-coverage or probability guarantee."""
    def __init__(self, capacity: int = 180, minimum: int = 60):
        self.capacity, self.minimum = capacity, minimum
        self.records: list[tuple[int, dict]] = []

    def add(self, target_close: int, errors: dict) -> None:
        self.records.append((int(target_close), errors))

    def factors(self, asof: int) -> tuple[dict, int]:
        # This filter also protects use when forecast labels arrive with a delay.
        past = [e for t, e in self.records if t <= asof][-self.capacity:]
        if len(past) < self.minimum:
            return {c: 0.0 for c in LEVELS}, len(past)
        n = len(past)
        result = {}
        for c in LEVELS:
            k = min(n, math.ceil((n + 1) * c))
            result[c] = float(np.sort([e[c] for e in past])[k - 1])
        return result, n


def summarize_distribution(paths: np.ndarray, price: np.ndarray, asof: int,
                           model: str, diagnostics: dict,
                           correction: dict | None = None) -> dict:
    diagnostics = dict(diagnostics)
    if model == "historical":
        diagnostics["weight_effective_support"] = float(diagnostics["eligible_historical_paths"])
        diagnostics["uniform_tail_weight"] = 1.0
    elif model == "gaussian":
        diagnostics["weight_effective_support"] = None
        diagnostics["uniform_tail_weight"] = None
        diagnostics["support_warning"] = "Gaussian innovations do not sample historical paths"
    result = {"schema_version": "liqzo.forecast.v0.1", "model_version": VERSION,
              "model": model, "mode": "research_snapshot", "execution_allowed": False,
              "signal": "NO_TRADE", "validation_status": "UNVALIDATED",
              "as_of": iso(asof), "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
              "venue": "Binance USD-M", "price_kind": "hourly close",
              "assets": {}, "paths": len(paths), "diagnostics": diagnostics,
              "quantile_semantics": "pointwise hourly-close intervals; not whole-path containment",
              "calibration_semantics": "raw model event probabilities; optional correction only widens terminal intervals",
              "freshness": "historical data snapshot; live publication prohibited"}
    for ai, asset in enumerate(ASSETS):
        prices = price[ai] * np.exp(paths[:, :, ai])
        horizon_data = {}
        for h in HORIZONS:
            terminal = paths[:, h - 1, ai]
            quantiles = np.quantile(prices[:, h - 1], [0.025, 0.1, 0.25, 0.5, 0.75, 0.9, 0.975])
            # +/-0.25% is an illustrative event definition, NOT a cost or entry threshold.
            up = float(np.mean(terminal > np.log1p(0.0025)))
            down = float(np.mean(terminal < np.log1p(-0.0025)))
            horizon_data[str(h)] = {"target_close": iso(asof + h * 3600),
                "q025_q10_q25_q50_q75_q90_q975": quantiles.tolist(),
                "p_endpoint_up_gt_0_25pct": up, "p_endpoint_down_gt_0_25pct": down,
                "p_endpoint_flat_within_0_25pct": 1 - up - down,
                "p_endpoint_positive": float(np.mean(terminal > 0)),
                "p_hourly_close_ever_up_2pct": float(np.mean((prices[:, :h] >= price[ai] * 1.02).any(axis=1))),
                "p_hourly_close_ever_down_2pct": float(np.mean((prices[:, :h] <= price[ai] * 0.98).any(axis=1))),
                "mc_probability_se_upper_bound": float(0.5 / math.sqrt(len(paths)))}
            if correction and (asset, h) in correction:
                horizon_data[str(h)]["corrected_endpoint_intervals"] = correction[(asset, h)]
        result["assets"][asset] = {"origin_close": float(price[ai]), "horizons_hours": horizon_data,
            "fan_hourly": {"hours": list(range(1, 25)),
                "quantile_levels": [0.025, 0.1, 0.25, 0.5, 0.75, 0.9, 0.975],
                "prices": np.quantile(prices, [0.025, 0.1, 0.25, 0.5, 0.75, 0.9, 0.975], axis=0).tolist()}}
    return result


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
