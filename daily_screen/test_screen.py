#!/usr/bin/env python3
"""Standalone pre-publish and negative tests. Works with Python stdlib only."""
from __future__ import annotations
import argparse
from collections import defaultdict
from datetime import datetime,timezone,timedelta
import json
from pathlib import Path
import tempfile
import sys
import screen


def verify_output(base):
    rows=screen.load_history(base/'history.csv')
    record=json.loads((base/'latest.json').read_text(encoding='utf-8'))
    trade,days,items,stats=screen.screened(rows)
    assert record['AsOfDate']==trade, 'SAVED_DATE_NOT_MATCHING_RAW'
    assert record['LastFiveSessions']==days, 'SAVED_FIVE_SESSIONS_NOT_MATCHING_RAW'
    assert record['Counts']==stats, 'SAVED_COUNTS_NOT_MATCHING_SCREEN'
    assert record['Candidates']==items, 'SAVED_CANDIDATES_NOT_MATCHING_SCREEN'
    hashes=defaultdict(set)
    for row in rows:
        hashes[(row['Date'],row['Market'])].add(row['SourceSHA256'])
    assert all(len(v)==1 for v in hashes.values()), 'MULTIPLE_OFFICIAL_SOURCE_HASHES_SAME_DAY_MARKET'
    assert record['UniverseScope']=='TWSE+TPEx 4-digit ordinary stocks'
    assert record['Strategy'].startswith('5-session technical OBSERVATION ONLY')
    if trade=='2026-10-16':
        assert stats['CompleteFiveSessionStocks']==1901, stats
        assert stats['BreakoutCount']==12,stats
        assert stats['TrendCount']==5,stats
        assert len(items)==17
        assert hashes[('2026-10-16','TWSE')]=={'7a8575b374e2b7993f9fdcaeca5dbe9e586bee69d0288754137442d40644aa1e'}
        assert hashes[('2026-10-16','TPEx')]=={'aca991ce5dfa76c73c97bea3072b22dd85842f9c975930f4de686188bc6958fd'}
    print('VERIFIED_OUTPUT',trade,'candidates',len(items),'sessions',days)


def test_parser():
    assert screen.is_stock('2330') and not screen.is_stock('0050')
    assert screen.value('1,234.50')==1234.5
    assert screen.value('--') is None
    assert screen.normalized_header(' 成交股數  ' )=='成交股數'
    assert screen.normalized_header('最後買量<br>(張數)')=='最後買量(張數)'
    day='2026-10-16'
    def table(market):
        if market=='TWSE':
            names=['證券代號','證券名稱','成交股數','開盤價','最高價','最低價','收盤價']
            title='115年10月16日 每日收盤行情(全部)'
        else:
            names=['代號','名稱','成交股數  ','開盤 ','最高 ','最低','收盤 ']
            title='上櫃股票每日收盤行情(不含定價)'
        data=[[str(i),'測試股','2,000,000','19.00','20.00','18.00','19.50'] for i in range(1000,1990)]
        return {'title':title,'fields':names,'data':data}
    for market in ('TWSE','TPEx'):
        tab=table(market)
        doc={'date':'20261016','stat':'ok','tables':[tab]}
        if market=='TPEx':doc['tables'].append({'title':'管理股票','fields':tab['fields'],'data':[]})
        b=json.dumps(doc,ensure_ascii=False).encode()
        parsed=screen.parse_payload(b,market,day)
        assert len(parsed)==990 and parsed['1101']['Volume']==2_000_000
        def must_fail(mut,code):
            try:screen.parse_payload(json.dumps(mut,ensure_ascii=False).encode(),market,day)
            except screen.SourceError as exc:
                assert code in str(exc),(code,str(exc));return
            raise AssertionError('FAIL_GATE_'+code)
        mut=dict(doc,date='20261015');must_fail(mut,'SOURCE_DATE_MISMATCH')
        mut=dict(doc,stat='bad');must_fail(mut,'OFFICIAL_NO_SESSION')
        mut=dict(doc,tables=[tab,tab]);must_fail(mut,'OFFICIAL_TABLE_AMBIGUOUS')
        mut=dict(doc,tables=[dict(tab,data=tab['data']+[tab['data'][0]])]);must_fail(mut,'DUPLICATE_CODE')
        mut=dict(doc,tables=[dict(tab,data=[x[:-1] for x in tab['data']])]);must_fail(mut,'OFFICIAL_BAD_ROW_LENGTH')
        mut=dict(doc,tables=[dict(tab,data=[x[:-1]+['--'] for x in tab['data']])]);must_fail(mut,'OFFICIAL_COVERAGE_LOW')
    print('TEST_PARSE_NEGATIVE=PASS')


def test_history_merge(base):
    rows=screen.load_history(base/'history.csv')
    merged=screen.merge_history(rows,[])
    assert len(merged)==len(rows)
    assert merged[-1]['Date']==max(r['Date'] for r in rows)
    bad=rows[0].copy();bad['High']='0'
    try:screen.verify_rows([bad]);raise AssertionError('BAD_OHLC_ALLOWED')
    except screen.SourceError:pass
    try:screen.verify_rows([rows[0],rows[0]]);raise AssertionError('DUPLICATES_ALLOWED')
    except screen.SourceError:pass
    def fn(market,day):
        if market=='TWSE':return {'1234':{'Market':'TWSE','Code':'1234','Date':day}}
        raise screen.SourceError('TPEX_UNAVAILABLE')
    try:
        screen.latest_official(now=datetime(2026,10,16,20,tzinfo=timezone(timedelta(hours=8))),get=fn)
        raise AssertionError('SILENT_MARKET_MIX')
    except screen.SourceError as e:assert 'TPEX_UNAVAILABLE' in str(e)
    print('TEST_MERGE_FRESHNESS=PASS')


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--verify-only',action='store_true')
    ap.add_argument('--base',type=Path,default=Path(__file__).resolve().parent)
    args=ap.parse_args()
    verify_output(args.base)
    if not args.verify_only:
        test_parser();test_history_merge(args.base)
    print('YHTIS_DAILY_CHECKS_PASS')

if __name__=='__main__':
    try:main()
    except Exception as e:
        print('TEST FAILED:',repr(e),file=sys.stderr);sys.exit(2)
