#!/usr/bin/env python3
"""YHTIS daily, official-raw-only, FIVE-SESSION observation list.

Isolated sidecar. Never reads/writes Golden Entry, AI scores, project History or index.html.
No external Python packages. CI safe: failure leaves all published outputs untouched.
"""
from __future__ import annotations
import argparse
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

COLUMNS = ['Date','Market','Code','Name','Open','High','Low','Close','Volume','SourceSHA256']
MAX_BYTES=14_000_000
MIN_COUNTS={'TWSE':950,'TPEx':700}
TIMEZONE_TW=timezone(timedelta(hours=8))

class SourceError(RuntimeError): pass
class NoSession(SourceError): pass

def is_stock(code):return bool(re.fullmatch(r'[1-9]\d{3}',str(code)))
def value(v):
    v=str(v).strip().replace(',','')
    if not v or v in {'--','---','－','-','N/A','X'}:return None
    try:
        n=float(v)
        return n if math.isfinite(n) else None
    except (TypeError,ValueError):return None

def urls(market,day):
    d=datetime.strptime(day,'%Y-%m-%d')
    if market=='TWSE':
        q=urllib.parse.urlencode({'date':d.strftime('%Y%m%d'),'type':'ALLBUT0999','response':'json'})
        return [f'https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?{q}',
                f'https://www.twse.com.tw/exchangeReport/MI_INDEX?{q}']
    q=urllib.parse.urlencode({'date':d.strftime('%Y/%m/%d'),'id':'','response':'json'})
    roc=f'{d.year-1911:03d}/{d.month:02d}/{d.day:02d}'
    old=urllib.parse.urlencode({'l':'zh-tw','d':roc,'se':'EW','o':'json'})
    return [f'https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?{q}',
            f'https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php?{old}']

def fetch(url):
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 (compatible; YHTIS-DailyScreen/1.0)',
        'Accept':'application/json','Referer':'https://www.tpex.org.tw/' if 'tpex.org.tw' in url else 'https://www.twse.com.tw/'})
    errors=[]
    try:
        with urllib.request.urlopen(req,timeout=25) as resp:
            if resp.status!=200: raise SourceError('HTTP_STATUS_'+str(resp.status))
            b=resp.read(MAX_BYTES+1)
        if not 0<len(b)<=MAX_BYTES:raise SourceError('BAD_RESPONSE_LENGTH')
        return b
    except Exception as ex:
        errors.append(type(ex).__name__+':'+str(ex)[:160])
    try:
        # curl is available on macOS and ubuntu-latest; TLS is NOT bypassed.
        p=subprocess.run(['curl','--fail','--location','--silent','--show-error','--compressed',
                          '--connect-timeout','15','--max-time','45','--retry','1',url],
                         capture_output=True,timeout=100)
        if p.returncode!=0 or not 0<len(p.stdout)<=MAX_BYTES:
            raise SourceError('CURL_'+str(p.returncode)+' '+p.stderr.decode('utf-8','replace')[:130])
        return p.stdout
    except Exception as ex:
        errors.append(type(ex).__name__+':'+str(ex)[:160])
    raise SourceError('NETWORK_FAILED '+';'.join(errors))

def normalized_header(s):
    return re.sub(r'<[^>]*>','',str(s)).replace(' ','').replace('\u3000','').strip()

def parse_payload(raw,market,day):
    try:doc=json.loads(raw)
    except Exception as e:raise SourceError('JSON_INVALID') from e
    if not isinstance(doc,dict):raise SourceError('JSON_NOT_OBJECT')
    if str(doc.get('date',''))!=day.replace('-',''):
        raise SourceError('SOURCE_DATE_MISMATCH '+str(doc.get('date')))
    status=str(doc.get('stat','')).lower()
    if status!='ok':raise NoSession('OFFICIAL_NO_SESSION '+status[:80])
    expected={'TWSE':['證券代號','證券名稱','開盤價','最高價','最低價','收盤價','成交股數'],
              'TPEx':['代號','名稱','開盤','最高','最低','收盤','成交股數']}[market]
    eligible=[]
    for t in doc.get('tables',[]):
        if not isinstance(t,dict):continue
        title=str(t.get('title',''))
        if market=='TWSE':title_ok='每日收盤行情' in title
        else:title_ok='上櫃股票' in title and '管理股票' not in title and '定價交易' not in title
        fields=[normalized_header(x) for x in t.get('fields',[])]
        if title_ok and len(t.get('data',[]))>0 and all(x in fields for x in expected):
            eligible.append((fields,t))
    if len(eligible)!=1:raise SourceError(f'OFFICIAL_TABLE_AMBIGUOUS_{market}_{len(eligible)}')
    fields,table=eligible[0]
    col={name:fields.index(name) for name in expected}
    mapcol={'Code':'證券代號' if market=='TWSE' else '代號',
            'Name':'證券名稱' if market=='TWSE' else '名稱',
            'Open':'開盤價' if market=='TWSE' else '開盤',
            'High':'最高價' if market=='TWSE' else '最高',
            'Low':'最低價' if market=='TWSE' else '最低',
            'Close':'收盤價' if market=='TWSE' else '收盤',
            'Volume':'成交股數'}
    found={}
    sha=hashlib.sha256(raw).hexdigest()
    for arr in table['data']:
        if not isinstance(arr,list) or len(arr)!=len(fields):raise SourceError('OFFICIAL_BAD_ROW_LENGTH')
        code=str(arr[col[mapcol['Code']]]).strip()
        if not is_stock(code):continue
        if code in found:raise SourceError('DUPLICATE_CODE_'+code)
        numerics={field:value(arr[col[mapcol[field]]]) for field in ('Open','High','Low','Close','Volume')}
        if any(n is None for n in numerics.values()):continue
        o,h,l,c,v=[numerics[x] for x in ('Open','High','Low','Close','Volume')]
        if not (v>0 and l>0 and l<=min(o,c)<=h and h>=max(o,c)):continue
        found[code]=dict(Date=day,Market=market,Code=code,
                         Name=str(arr[col[mapcol['Name']]]).strip(),
                         **numerics,SourceSHA256=sha)
    if len(found)<MIN_COUNTS[market]:raise SourceError(f'OFFICIAL_COVERAGE_LOW_{market}_{len(found)}')
    return found

def get_official(market,day,request=fetch):
    errs=[];only_no_session=True
    for url in urls(market,day):
        try:return parse_payload(request(url),market,day)
        except NoSession as e:errs.append(str(e))
        except Exception as e:
            only_no_session=False;errs.append(type(e).__name__+':'+str(e)[:130])
    if only_no_session:raise NoSession(' | '.join(errs))
    raise SourceError(f'{market} {day}: '+' | '.join(errs))

def latest_official(now=None,get=get_official):
    now=now or datetime.now(TIMEZONE_TW)
    d=now.date() if now.hour>=18 else (now.date()-timedelta(days=1))
    for k in range(12):
        day=(d-timedelta(days=k)).isoformat()
        if datetime.strptime(day,'%Y-%m-%d').weekday()>=5:continue
        try:tw=get('TWSE',day)
        except NoSession:continue
        # If TWSE has a valid current session but TPEx fails, NEVER substitute prior day's TPEx.
        tp=get('TPEx',day)
        keys=set(tw).intersection(tp)
        if keys:raise SourceError('CROSS_MARKET_DUPLICATE_CODES '+str(sorted(keys)[:10]))
        merged=list(tw.values())+list(tp.values())
        if len(merged)<1700:raise SourceError('COMBINED_COVERAGE_LOW')
        return day,merged
    raise SourceError('NO_VERIFIED_RECENT_TWSE_SESSION')

def verify_rows(rows):
    seen=set()
    for r in rows:
        key=(r['Date'],r['Market'],r['Code'])
        if key in seen:raise SourceError('DUPLICATE_STOCK_DATE_MARKET_'+str(key))
        seen.add(key)
        if not is_stock(r['Code']):raise SourceError('INVALID_STOCK_CODE')
        if not re.fullmatch('[a-f0-9]{64}',r['SourceSHA256']):raise SourceError('NO_SOURCE_HASH')
        o,h,l,c,v=[float(r[x]) for x in ['Open','High','Low','Close','Volume']]
        if not all(math.isfinite(x) for x in [o,h,l,c,v]) or not (0<l<=min(o,c) and max(o,c)<=h and v>0):
            raise SourceError('INVALID_OHLCV')

def load_history(path):
    if not path.exists():raise SourceError('HISTORY_SEED_MISSING')
    with path.open(encoding='utf-8-sig',newline='') as f:
        rows=list(csv.DictReader(f))
    if not rows or set(rows[0])!=set(COLUMNS):raise SourceError('HISTORY_SCHEMA_INVALID')
    verify_rows(rows)
    return rows

def merge_history(old,new):
    # Do not cross-stitch market transfers; tracked by Date+Market+Code.
    merged={(r['Date'],r['Market'],r['Code']):r for r in old}
    for r in new:merged[(r['Date'],r['Market'],r['Code'])]=r
    days=sorted({k[0] for k in merged})[-30:]
    updated=[r for k,r in merged.items() if k[0] in days]
    updated.sort(key=lambda r:(r['Date'],r['Market'],r['Code']))
    verify_rows(updated)
    return updated

def screened(rows):
    days=sorted({r['Date'] for r in rows})
    if len(days)<5:raise SourceError('NEED_AT_LEAST_FIVE_TRADING_SESSIONS')
    five=days[-5:];today=five[-1]
    by={}
    for r in rows:
        if r['Date'] in five:
            by.setdefault((r['Market'],r['Code']),{})[r['Date']]=r
    out=[]; coverage=0;abnormal=0
    for (market,code),hist in by.items():
        if len(hist)!=5:continue
        series=[hist[d] for d in five]
        p=[float(r['Close']) for r in series]
        v=[float(r['Volume']) for r in series]
        hi=[float(r['High']) for r in series]
        lo=[float(r['Low']) for r in series]
        op=[float(r['Open']) for r in series]
        if any(p[i]/p[i-1]-1>0.35 or p[i]/p[i-1]-1<-.35 for i in range(1,5)):
            abnormal+=1;continue
        coverage+=1
        if not (15<=p[-1]<=300 and v[-1]>=1_000_000):continue
        vavg=sum(v[:4])/4
        vr=v[-1]/vavg if vavg else 0
        upper=(p[-1]-lo[-1])/(hi[-1]-lo[-1]) if hi[-1]>lo[-1] else 0
        risk=100*(p[-1]-lo[-1])/p[-1]
        daily=100*(p[-1]/p[-2]-1)
        ret5=100*(p[-1]/p[0]-1)
        if risk>5 or daily>=9.8 or daily<=-5 or upper<0.70:continue
        breakout=(p[-1]>max(hi[:4]) and p[-1]>op[-1] and vr>=1.5 and daily>0)
        trend=(p[-1]>p[-2] and p[-1]>sum(p[1:4])/3 and p[-1]>op[-1]
               and vr>=1.15 and ret5>1.5)
        category='突破觀察' if breakout else ('趨勢觀察' if trend else None)
        if not category:continue
        out.append({'Code':code,'Name':series[-1]['Name'],'Market':market,'Category':category,
            'Close':round(p[-1],2),'DayPct':round(daily,2),'FiveDayPct':round(ret5,2),
            'VolumeLots':round(v[-1]/1000),'VolumeRatio':round(vr,2),
            'ClosePositionPct':round(upper*100,1),'ReferenceLow':round(lo[-1],2),
            'RiskToDayLowPct':round(risk,2),
            'Note':'短週期條件篩選，並非買進訊號或 AI 預測'})
    out.sort(key=lambda a:(a['Category']!='突破觀察',-a['VolumeRatio'],-a['FiveDayPct'],a['Code']))
    return today,five,out[:60],{'CompleteFiveSessionStocks':coverage,'ExcludedExtremePriceJump':abnormal,
         'BreakoutCount':sum(x['Category']=='突破觀察' for x in out),
         'TrendCount':sum(x['Category']=='趨勢觀察' for x in out),
         'TotalCandidates':len(out)}

def csv_bytes(rows,fields):
    s=io.StringIO();w=csv.DictWriter(s,fieldnames=fields);w.writeheader();w.writerows(rows)
    return s.getvalue().encode('utf-8-sig')

def main(argv=None):
    ap=argparse.ArgumentParser()
    ap.add_argument('--mode',choices=['seed','live'],default='live')
    ap.add_argument('--base',type=Path,default=Path(__file__).resolve().parent)
    args=ap.parse_args(argv)
    base=args.base
    old=load_history(base/'history.csv')
    existing_max=max(r['Date'] for r in old)
    if args.mode=='live':
        date,items=latest_official()
        print('OFFICIAL_EOD=',date,'COUNT=',len(items),flush=True)
        if date<existing_max:raise SourceError('OFFICIAL_OLDER_THAN_LOCAL_SNAPSHOT')
        if date==existing_max:
            print('NO_NEW_SESSION: existing valid snapshot unchanged',flush=True)
            return 0
        rows=merge_history(old,items)
    else:
        rows=old
    trade_date,five,filtered,stats=screened(rows)
    doc={'AsOfDate':trade_date,'LastFiveSessions':five,'UniverseScope':'TWSE+TPEx 4-digit ordinary stocks',
       'Source':'TWSE/TPEx dated official daily OHLCV (unadjusted)',
       'Strategy':'5-session technical OBSERVATION ONLY — not Golden Entry, Stage1, Stage2, or investment advice',
       'Disclaimer':'非必漲預測。未提供勝率驗證。價格可能涉及除權、分割；個別異常股已先排除。',
       'GeneratedTaipei':datetime.now(TIMEZONE_TW).isoformat(timespec='seconds'),
       'Counts':stats,'Candidates':filtered}
    # Compute everything & validate BEFORE touching any published file.
    payload={'history.csv':csv_bytes(rows,COLUMNS),
       'latest.json':(json.dumps(doc,ensure_ascii=False,indent=2)+'\n').encode(),
       'screen.csv':csv_bytes(filtered,list(filtered[0]) if filtered else ['Code','Name','Market','Category','Close','DayPct','FiveDayPct','VolumeLots','VolumeRatio','ClosePositionPct','ReferenceLow','RiskToDayLowPct','Note'])}
    base.mkdir(parents=True,exist_ok=True)
    for name,content in payload.items():
        p=base/name
        temp=p.with_suffix(p.suffix+'.new')
        temp.write_bytes(content)
        temp.replace(p)
    print(json.dumps({'date':trade_date,'covered':stats['CompleteFiveSessionStocks'],
                      'breakout':stats['BreakoutCount'],'trend':stats['TrendCount'],
                      'presented':len(filtered),'market_days':five},ensure_ascii=False))
    return 0

if __name__=='__main__':
    try:sys.exit(main())
    except Exception as e:
        print('YHTIS SCREEN ABORT (published data kept): '+type(e).__name__+': '+str(e),file=sys.stderr)
        sys.exit(2)
