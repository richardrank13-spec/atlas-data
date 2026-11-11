from dataclasses import dataclass
from pathlib import Path
import hashlib
import numpy as np
import pandas as pd

ROOT=Path(__file__).parent
Q=np.arange(1,10)/10
ASSETS=('BTC','ETH','BNB','SOL','LINK','LTC','ZEC','NASDAQ100','SP500')
PRIMARY=('BTC','ETH')
MODELS=('gaussian','historical','conditional','ewma_fhs','garch_t','gjr_t','quantile_hgb','equal_pool','chronos2_small')
QC=[f'q{i}' for i in range(10,100,10)]

@dataclass
class Data:
    name:str
    time:np.ndarray
    close:np.ndarray
    r:np.ndarray
    sigma:np.ndarray
    slow:np.ndarray
    features:np.ndarray
    analog_features:np.ndarray
    fast:int
    slow_window:int
    window:int
    horizons:tuple
    quality:dict

def load(name):
    primary=name in PRIMARY
    if name in ('NASDAQ100','SP500'):
        f=pd.read_csv(ROOT/'data'/('yahoo_ndx_1d.csv' if name=='NASDAQ100' else 'yahoo_spx_1d.csv'))
        f['time']=f.t.astype('int64')+86400
        f['taker_buy_volume']=f.volume/2
        f=f.drop(columns=['t'])
    else:
        paths=sorted((ROOT/'data').glob(f'um_{name.lower()}usdt_1h_*.csv'))
        f=pd.concat([pd.read_csv(p) for p in paths],ignore_index=True)
        assert not f.open_time.duplicated().any()
        assert np.all(np.diff(f.open_time)>0)
        if primary:
            f=f.set_index('open_time').reindex(np.arange(f.open_time.min(),f.open_time.max()+1,3600))
            f['time']=f.index.to_numpy()+3600
        else:
            f['day']=(f.open_time//86400)*86400
            g=f.groupby('day').agg(open=('open','first'),high=('high','max'),low=('low','min'),close=('close','last'),volume=('volume','sum'),taker_buy_volume=('taker_buy_volume','sum'),count=('close','count'))
            g.loc[g['count']!=24,['open','high','low','close','volume','taker_buy_volume']]=np.nan
            f=g.reindex(np.arange(g.index.min(),g.index.max()+1,86400))
            f['time']=f.index.to_numpy()+86400
    assert np.all(np.diff(f.time)>0)
    c=f.close.to_numpy(float); r=np.r_[np.nan,np.diff(np.log(c))]
    fast,slow=(24,168) if primary else (20,60)
    s=pd.Series(r)
    sig=np.sqrt(s.pow(2).rolling(fast).mean()).to_numpy()
    sigslow=np.sqrt(s.pow(2).rolling(slow).mean()).to_numpy()
    mom=s.rolling(fast).sum().to_numpy()/(sigslow*np.sqrt(fast))
    ratio=np.log(sig/sigslow)
    v=pd.Series(f.volume.to_numpy(float));tv=pd.Series(f.taker_buy_volume.to_numpy(float))
    imb=((2*tv-v).rolling(fast).sum()/v.rolling(fast).sum().replace(0,np.nan)).to_numpy()
    analog=np.column_stack([mom,ratio,imb])
    features=np.column_stack([mom,ratio,imb,r/sig,np.log(f.high.to_numpy()/f.low.to_numpy())/sig,
        np.log((v+1e-8)/(v.rolling(fast).mean()+1e-8)),s.rolling(slow).sum()/(sigslow*np.sqrt(slow))])
    return Data(name,f.time.to_numpy('int64'),c,r,sig,sigslow,features,analog,fast,slow,
                365*24 if primary else 750,(4,24) if primary else (1,),
                {'rows':len(f),'missing_closes':int(np.isnan(c).sum()),'first_available':iso(f.time.iloc[0]),'last_available':iso(f.time.iloc[-1]),'index_availability':'session date + 1 UTC day' if name in ('NASDAQ100','SP500') else 'closed candle'})

def iso(t):return pd.Timestamp(int(t),unit='s',tz='UTC').isoformat()

def origins(d,phase):
    start,end=('2025-01-01','2026-01-01') if phase=='selection' else ('2026-01-01','2026-10-28')
    a,b=int(pd.Timestamp(start,tz='UTC').timestamp()),int(pd.Timestamp(end,tz='UTC').timestamp())
    cand=np.flatnonzero((d.time>=a)&(d.time<b)&(d.time%86400==0))
    good=[];bad=[]
    for i in cand:
        h=max(d.horizons)
        if i+h>=len(d.close) or not np.isfinite(d.features[i]).all() or not np.isfinite(d.r[i+1:i+h+1]).all():bad.append(iso(d.time[i]))
        else:good.append(i)
    return np.array(good,int),bad

def seed(name,model,t):return int.from_bytes(hashlib.sha256(f'{name}|{model}|{int(t)}|1729'.encode()).digest()[:4],'little')

def weighted_quantile(values,w):
    j=np.argsort(values);x=values[j];w=w[j];cs=np.cumsum(w);cs/=cs[-1]
    return x[np.minimum(np.searchsorted(cs,Q),len(x)-1)]

def wis(y,q):
    e=np.asarray(y)[...,None]-np.asarray(q)
    return 2*np.mean(np.maximum(Q*e,(Q-1)*e),axis=-1)

def regime(joint,i):
    f=joint.features[i];return ('up' if f[0]>=0 else 'down')+'_'+('high_vol' if f[2]>=0 else 'low_vol')

def record(d,i,h,model,q,reg):
    q=np.asarray(q,float)
    if q.shape!=(9,) or not np.isfinite(q).all() or np.any(np.diff(q)<0):raise ValueError('invalid quantiles')
    y=float(np.log(d.close[i+h]/d.close[i]))
    return dict(asset=d.name,origin=iso(d.time[i]),target=iso(d.time[i+h]),horizon=h,model=model,y=y,
                wis=float(wis(y,q)),covered80=bool(q[0]<=y<=q[-1]),width80=float(q[-1]-q[0]),regime=reg,**dict(zip(QC,q.tolist())))
