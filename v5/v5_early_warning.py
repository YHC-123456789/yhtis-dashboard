#!/usr/bin/env python3
"""YHTIS V5 early warning (research preview, standard library only).
Run from repository root: python3 v5/v5_early_warning.py
Reads stock_query/stocks/*.json and official EOD. Does NOT edit original data.
No Golden Entry / Stage1 / Stage2 outputs are computed or changed.
"""
from __future__ import annotations
import json, math, statistics
from collections import Counter
from pathlib import Path
from datetime import date

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'output/official_market_eod_latest.json'
STOCK_DIR = ROOT / 'stock_query/stocks'
OUT = ROOT / 'v5/research_preview/early_warning.json'
MIN_COVERAGE = .80
MIN_BARS = 60
KEYS = ('開盤', '最高', '最低', '收盤', '成交量')

def number(x):
    try:
        v = float(str(x).replace(',', '').strip())
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None

def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))

def valid_bar(b):
    try:
        date.fromisoformat(b['日期'])
        o,h,l,c,v = [number(b.get(k)) for k in KEYS]
        return all(x is not None for x in (o,h,l,c,v)) and 0 < l <= min(o,c) <= max(o,c) <= h and v > 0
    except (KeyError, ValueError, TypeError):
        return False

def ema(values, span):
    x = values[0]
    alpha = 2/(span+1)
    for v in values[1:]:
        x = alpha*v+(1-alpha)*x
    return x

def kd(bars):
    # Taiwan common 9,3,3 RSV/K/D, initialized at 50.
    k=d=50.
    prev_j=None
    for i,b in enumerate(bars):
        window=bars[max(0,i-8):i+1]
        hh=max(x['最高'] for x in window)
        ll=min(x['最低'] for x in window)
        rsv=(b['收盤']-ll)/(hh-ll)*100 if hh>ll else 50.
        k=(2*k+rsv)/3
        d=(2*d+k)/3
        j=3*k-2*d
        if i==len(bars)-2:
            prev_j=j
    return k,d,j,prev_j

def compute(bars):
    closes=[b['收盤'] for b in bars]
    volumes=[b['成交量'] for b in bars]
    # OBV: rising total over last 10 sessions, not an invented institutional flow.
    obv=[0.]
    for i in range(1,len(bars)):
        sign=(closes[i]>closes[i-1])-(closes[i]<closes[i-1])
        obv.append(obv[-1]+sign*volumes[i])
    widths=[]
    for i in range(19,len(closes)):
        window=closes[i-19:i+1]
        ma=statistics.mean(window)
        widths.append(4*statistics.pstdev(window)/ma if ma else float('inf'))
    current_width=widths[-1]
    typical_width=statistics.median(widths[-20:])
    k,d,j,previous_j=kd(bars)
    vol_ratio=volumes[-1]/statistics.mean(volumes[-6:-1]) if statistics.mean(volumes[-6:-1])>0 else None
    prior_20_high=max(x['最高'] for x in bars[-21:-1])
    return {
        'Return5Pct':round((closes[-1]/closes[-6]-1)*100,3),
        'Return20Pct':round((closes[-1]/closes[-21]-1)*100,3),
        'OBV10Change':round(obv[-1]-obv[-11],0),
        'OBV10Rising':obv[-1]>obv[-11],
        'BBW20':round(current_width,5),
        'BBWvsRecentMedian':round(current_width/typical_width,3) if typical_width>0 else None,
        'BBWContracting':current_width<typical_width*.85,
        'K':round(k,2),'D':round(d,2),'J':round(j,2),
        'JTurningUp':previous_j is not None and j>previous_j and previous_j<50,
        'VolumeRatio5':round(vol_ratio,3) if vol_ratio else None,
        'NearPrior20High':closes[-1]>=prior_20_high*.97 and closes[-1]<prior_20_high*1.02,
        'EMA9Above20':ema(closes,9)>ema(closes,20),
    }

def main():
    if not SOURCE.is_file() or not STOCK_DIR.is_dir():
        raise RuntimeError('找不到官方 EOD 或個股歷史資料；請在 YHTIS 專案中執行')
    eod=read_json(SOURCE)
    asof=eod['Date']
    date.fromisoformat(asof)
    if not isinstance(eod.get('Stocks'),list) or not eod['Stocks']:
        raise RuntimeError('官方股票清單為空')
    sources={(str(s['Code']),str(s.get('Market',''))):s for s in eod['Stocks'] if str(s.get('Code','')).isdigit()}
    unique={str(s['Code']):s for s in eod['Stocks'] if str(s.get('Code','')).isdigit() and len(str(s.get('Code','')))==4}
    stats=Counter()
    vetted=[]
    for code,s in unique.items():
        path=STOCK_DIR/f'{code}.json'
        if not path.is_file():
            stats['missing_file']+=1
            continue
        try:
            data=read_json(path)
            raw=data['K線']
            if not isinstance(raw,list) or len(raw)<MIN_BARS:
                stats['short_history']+=1
                continue
            if any(not isinstance(b,dict) for b in raw):
                stats['invalid_history']+=1
                continue
            dates=[b.get('日期') for b in raw]
            if len(set(dates))!=len(dates) or dates!=sorted(dates):
                stats['unordered_or_duplicate']+=1
                continue
            if any(not valid_bar(b) for b in raw):
                stats['invalid_history']+=1
                continue
            if dates[-1]!=asof:
                stats['stale_history']+=1
                continue
            if len(raw)<MIN_BARS:
                stats['short_history']+=1
                continue
            if abs(raw[-1]['收盤']-number(s.get('Close')))>0.011 or abs(raw[-1]['成交量']-number(s.get('Volume')))>1:
                stats['eod_mismatch']+=1
                continue
            metrics=compute(raw)
            vetted.append({'Code':code,'Name':s.get('Name',''), 'Market':s.get('Market',''), 'Date':asof,**metrics})
        except (OSError,ValueError,TypeError,KeyError,ZeroDivisionError,OverflowError):
            stats['parse_error']+=1
    denominator=len(unique)
    coverage=len(vetted)/denominator if denominator else 0
    status='RESEARCH_PREVIEW_ONLY' if coverage>=MIN_COVERAGE and len(vetted)>=100 else 'BLOCKED_INSUFFICIENT_COVERAGE'
    candidates=[]
    if status=='RESEARCH_PREVIEW_ONLY':
        # Cross-sectional relative strength among same-day validated universe.
        med5=statistics.median(x['Return5Pct'] for x in vetted)
        med20=statistics.median(x['Return20Pct'] for x in vetted)
        for v in vetted:
            v['RS5vsMedian']=round(v['Return5Pct']-med5,3)
            v['RS20vsMedian']=round(v['Return20Pct']-med20,3)
            # Research heuristic; no historical win-rate claim.
            score=(v['RS5vsMedian']>0)*2+(v['RS20vsMedian']>0)*2+int(v['OBV10Rising'])*2+int(v['BBWContracting'])*2+int(v['JTurningUp'])+int(v['NearPrior20High'])+int(v['EMA9Above20'])
            v['ResearchScore']=score
            v['EligibleResearch']=score>=7 and v['VolumeRatio5'] is not None and v['VolumeRatio5']>=1
        candidates=sorted((v for v in vetted if v['EligibleResearch']),key=lambda v:(-v['ResearchScore'],-v['RS20vsMedian'],-v['RS5vsMedian'],v['Code']))[:10]
    report={
        'AsOfDate':asof, 'Status':status, 'Mode':'OBSERVATION_ONLY',
        'UniverseTotal':denominator,'ValidatedStocks':len(vetted),
        'CoveragePct':round(100*coverage,2), 'MinimumCoveragePct':MIN_COVERAGE*100,
        'Exclusions':dict(stats), 'Candidates':candidates,
        'Limitations':'日 K 資料須全筆合法且與官方最新日期、收盤、量一致；否則排除。RS 是同日合格樣本報酬相對中位數，非大盤指數 RS。未驗證收益或勝率；非 Golden Entry、Stage1、Stage2。',
    }
    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f'官方日期 {asof} | 合格 {len(vetted)}/{denominator} ({100*coverage:.1f}%)')
    print('狀態:',status)
    print('排除原因:',dict(stats))
    if status!='RESEARCH_PREVIEW_ONLY':
        print('⚠️ 當日資料完整率不足，禁止輸出 TOP 10。先修復資料更新流程。')
    else:
        for i,x in enumerate(candidates,1):
            print(f'{i:2d}. {x["Code"]} {x["Name"]} | 研究分 {x["ResearchScore"]} | RS20 {x["RS20vsMedian"]:+.2f} | 量比 {x["VolumeRatio5"]}')
        if not candidates:print('今日沒有符合條件的研究候選；不硬湊。')
    print('僅寫入',OUT.relative_to(ROOT),'｜未發布、未更動核心')

if __name__=='__main__':
    main()
