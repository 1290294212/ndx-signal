#!/usr/bin/env python3
"""NDX F-V1.0 reproducible signal calculator.
Data sources: FRED NASDAQ100 CSV (primary), yfinance ^NDX (fallback), AKShare .NDX (fallback).
Use --csv PATH to run offline from a two-column date/close CSV.
Decisions at the last trading close of each Friday-ending week; never same-close execution.
Not a trading system or broker interface.
"""
import argparse
import io
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

FRED_URL = 'https://fred.stlouisfed.org/graph/fredgraph.csv?id=NASDAQ100'


def normalise(frame, date='date', close='close'):
    z = frame[[date, close]].copy()
    z.columns = ['date', 'close']
    z['date'] = pd.to_datetime(z['date'], errors='coerce').dt.tz_localize(None).dt.normalize()
    z['close'] = pd.to_numeric(z['close'].replace('.', np.nan), errors='coerce')
    z = z.dropna().loc[lambda x: x.close > 0].drop_duplicates('date', keep='last').sort_values('date')
    if len(z) < 250:
        raise ValueError(f'Insufficient observations: {len(z)} (require >=250)')
    if (z.close.pct_change().abs() > 0.40).any():
        raise ValueError('Suspicious daily change >40%, inspect data / possible split')
    return z.set_index('date')['close'].astype(float)


def from_fred():
    import requests
    r = requests.get(FRED_URL, timeout=18, headers={'User-Agent':'Mozilla/5.0 NDX personal research'});
    r.raise_for_status()
    f = pd.read_csv(io.BytesIO(r.content))
    c = 'NASDAQ100' if 'NASDAQ100' in f.columns else 'value' if 'value' in f.columns else None
    d = 'observation_date' if 'observation_date' in f.columns else 'DATE' if 'DATE' in f.columns else None
    if not d or not c: raise ValueError(f'Unexpected FRED fields: {f.columns.tolist()}')
    return normalise(f, d, c)


def from_yahoo():
    import yfinance as yf
    f = yf.download('^NDX', period='5y', interval='1d', auto_adjust=False, progress=False, threads=False)
    if f.empty: raise ValueError('Yahoo returned empty table')
    if isinstance(f.columns, pd.MultiIndex):
        f.columns = f.columns.get_level_values(0)
    return normalise(f.reset_index(), 'Date', 'Close')


def from_akshare():
    import akshare as ak
    f = ak.index_us_stock_sina(symbol='.NDX')
    return normalise(f, 'date', 'close')


def load(path=None):
    if path:
        f=pd.read_csv(path)
        date = next((c for c in ['date','DATE','observation_date'] if c in f),None)
        close = next((c for c in ['close','NASDAQ100','Close','value'] if c in f),None)
        if not date or not close: raise ValueError('CSV requires date and close/NASDAQ100 columns')
        return normalise(f,date,close),'uploaded_csv',[]
    problems=[]; sources=[]
    for name, fn in [('fred',from_fred),('yfinance',from_yahoo),('akshare',from_akshare)]:
        try:
            s=fn(); sources.append((name,s))
        except Exception as exc:
            problems.append(f'{name}: {type(exc).__name__}: {str(exc)[:120]}')
    if not sources:raise RuntimeError('All data sources failed: '+' | '.join(problems))
    # Prefer latest source only if primary trails it by more than zero trading sessions.
    # Stitch more recent observations only after cross-checking overlapping close values.
    primary_name, primary = sources[0]
    for name, sec in sources[1:]:
        overlap=primary.index.intersection(sec.index)
        if len(overlap)>=20:
            deviations=((primary.loc[overlap[-20:]]/sec.loc[overlap[-20:]]-1).abs())
            if deviations.median()>0.001 or deviations.max()>0.015:
                problems.append(f'{name}: inconsistent overlapping closes; rejected')
                continue
        else:
            problems.append(f'{name}: too few overlap rows; rejected')
            continue
        if sec.index[-1]>primary.index[-1]:
            more=sec.loc[sec.index>primary.index[-1]]
            primary=pd.concat([primary,more]).sort_index()
            primary_name += '+'+name
    return primary,primary_name,problems


def calculate(close):
    ma=close.rolling(200,min_periods=200).mean()
    vol=close.pct_change().rolling(40,min_periods=40).std(ddof=1)*math.sqrt(252)
    # Historical hysteresis state, initialized down until 200d MA available.
    up=False; regimes=[]
    for val,m in zip(close.values,ma.values):
        if np.isfinite(m):
            if val>m*1.01: up=True
            elif val<m*.99: up=False
        regimes.append(up)
    regime=pd.Series(regimes,index=close.index)
    desired=(.10/vol).clip(upper=.60).fillna(0)
    desired=desired * regime.map({True:1.,False:.35})
    # Period W-FRI: only confirm a decision if this observation is the
    # scheduled FINAL exchange session of the week. Do not silently treat
    # a midweek/truncated data feed as a full week's Friday close.
    grp=close.index.to_period('W-FRI')
    week_ends=close.groupby(grp).tail(1).index
    calendar_warnings=[]
    weekly_rows=[]
    calendar_validated=[]
    fallback_used=[]
    try:
        import exchange_calendars as xcals
        cal=xcals.get_calendar('XNYS')
    except Exception as exc:
        cal=None
        calendar_warnings.append('exchange calendar unavailable: '+type(exc).__name__)
    for d in week_ends:
        period=d.to_period('W-FRI')
        if cal is not None:
            try:
                sessions=cal.sessions_in_range(period.start_time.normalize(),period.end_time.normalize())
                if len(sessions):
                    expected=sessions[-1].tz_localize(None).normalize()
                    if d == expected:
                        weekly_rows.append(d)
                        calendar_validated.append(d)
                    elif d < expected and d != week_ends[-1]:
                        calendar_warnings.append(f'{d.date()}: missing final session {expected.date()}; skipped decision')
                continue
            except Exception as exc:
                if len(calendar_warnings)<3:
                    calendar_warnings.append(f'{d.date()}: calendar unavailable ({type(exc).__name__})')
        # Fallback only: Friday close can be confirmed without holiday calendar;
        # do NOT label Thursday holiday closings as confirmed.
        if d.weekday()==4:
            weekly_rows.append(d)
            fallback_used.append(d)
    weekly_dates=pd.DatetimeIndex(weekly_rows)
    decisions=[]; last_target=0.0
    for date in weekly_dates:
        if not (np.isfinite(ma.loc[date]) and np.isfinite(vol.loc[date]) and vol.loc[date]>0):continue
        proposed=float(desired.loc[date]); moved=abs(proposed-last_target)>=.03-1e-12
        if moved: last_target=proposed
        decisions.append({'date':str(date.date()),'raw_weight':round(proposed,6),'target_weight':round(last_target,6),'adjust':bool(moved)})
    if not decisions:raise ValueError('Insufficient calculated weekly observations')
    date=close.index[-1]; latest_decision=decisions[-1]
    # Three independent facts: the latest observation is a weekly close,
    # the most recent decision date is validated, and data is up to date.
    latest_is_week_close=bool(date in weekly_dates)
    latest_week_period=date.to_period('W-FRI')
    weekly_period=weekly_dates[-1].to_period('W-FRI')
    res={
        'series':'NASDAQ100','last_observation_date':date.strftime('%Y-%m-%d'),
        'last_close':round(float(close.iloc[-1]),3),
        'sma200':round(float(ma.iloc[-1]),3),
        'deviation_from_sma200_pct':round(float((close.iloc[-1]/ma.iloc[-1]-1)*100),3),
        'volatility40_annual_pct':round(float(vol.iloc[-1])*100,3),
        'trend_state':'up' if regime.iloc[-1] else 'down',
        'preview_target_pct':round(float(desired.iloc[-1])*100,3),
        'most_recent_weekly_decision':latest_decision,
        'last_weekly_decision_validated':bool(pd.Timestamp(latest_decision['date']) in calendar_validated),
        'last_weekly_decision_calendar_fallback':bool(pd.Timestamp(latest_decision['date']) in fallback_used),
        'latest_observation_is_weekly_decision_close':latest_is_week_close,
        'current_observation_week_has_confirmed_close':bool(latest_week_period==weekly_period),
        'signal_explanation':('Latest observation is a validated weekly decision close.' if latest_is_week_close
                              else 'Current observation is a daily preview; last weekly decision stays in force.'),
        'calendar_warnings':calendar_warnings[:10],
        'base_capital_rmb':115000,
        'weekly_target_amount_rmb':round(115000*latest_decision['target_weight'],2),
        'data_points':len(close),
        'execution_note':'At earliest next market session; no automatic trades; check ETF premium and fund subscriptions',
    }
    return res


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--csv',help='Use local CSV date + close / NASDAQ100')
    ap.add_argument('--output',default='ndx_f_v1_signal.json')
    ap.add_argument('--allow-stale',action='store_true',help='Allow historic offline data for research only')
    args=ap.parse_args()
    s,source,problems=load(args.csv)
    age=(pd.Timestamp(datetime.now(timezone.utc).date())-s.index[-1]).days
    res=calculate(s);res['source']=source;res['source_warnings']=problems;res['age_calendar_days']=age
    res['stale']=age>5
    if res['stale'] and not args.allow_stale:
        res['trade_status']='DATA_STALE_NO_ACTION'
        res['execution_note']='Data stale: DO NOT act on calculated weights. Refresh market data first.'
    else:res['trade_status']='RESEARCH_SIGNAL_ONLY'
    Path(args.output).write_text(json.dumps(res,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(res,ensure_ascii=False,indent=2))
    if res['stale'] and not args.allow_stale:sys.exit(2)

if __name__=='__main__': main()
