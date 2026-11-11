#!/usr/bin/env python3
"""Research only: NDX close-to-close proxy, NOT executable ETF/fund NAV returns."""
import argparse
import io
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import requests

FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=NASDAQ100"


def normalize(df):
    date = next((c for c in ("DATE", "observation_date", "Date", "date") if c in df), None)
    close = next((c for c in ("NASDAQ100", "close", "Close", "value") if c in df), None)
    if not date or not close:
        raise ValueError("Expected date and NASDAQ100/close fields")
    s = pd.Series(pd.to_numeric(df[close], errors="coerce").to_numpy(),
                  index=pd.to_datetime(df[date], errors="coerce"))
    s = s.dropna()
    s = s[s > 0].sort_index()
    s = s[~s.index.duplicated(keep="last")]
    if len(s) < 500 or (s.pct_change().abs() > .4).any():
        raise ValueError("Insufficient/suspicious NDX daily prices")
    return s


def load_prices(path):
    if path:
        return normalize(pd.read_csv(path)), "local_csv"
    try:
        resp = requests.get(FRED, timeout=35, headers={"User-Agent": "NDX research"})
        resp.raise_for_status()
        return normalize(pd.read_csv(io.BytesIO(resp.content))), "FRED NASDAQ100"
    except Exception as e:
        print("FRED unavailable:", str(e)[:150], flush=True)
    import yfinance as yf
    df = yf.download("^NDX", start="1999-01-01", end="2026-01-01",
                     interval="1d", auto_adjust=False, progress=False, threads=False)
    if df.empty:
        raise ValueError("FRED failed and Yahoo ^NDX returned empty")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return normalize(df.reset_index()), "Yahoo ^NDX fallback (FRED unavailable)"


def signals(prices):
    sma200 = prices.rolling(200).mean()
    sma20 = prices.rolling(20).mean()
    sma50 = prices.rolling(50).mean()
    vol = prices.pct_change().rolling(40).std(ddof=1) * np.sqrt(252)
    state = False
    regimes = []
    for value, mean in zip(prices, sma200):
        if np.isfinite(mean):
            if value > mean * 1.01:
                state = True
            elif value < mean * .99:
                state = False
        regimes.append(state)
    bull = pd.Series(regimes, index=prices.index)
    base = (.10 / vol).clip(upper=.60).replace([np.inf, -np.inf], np.nan).fillna(0)
    base = base * np.where(bull, 1., .35)
    # Hypothesis, not optimized: conditional half-sizing, never forced to zero.
    enhanced = base * np.where((prices >= sma20) & (sma20 >= sma50), 1., .5)
    return base, enhanced


def decision_dates(index):
    # An actual exchange calendar avoids treating a truncated midweek feed as a week close.
    import exchange_calendars as xcals
    # exchange_calendars defaults to a rolling recent history (~20 years).
    # Explicit bounds are required for the 1999-2025 backtest.
    start = (index.min() - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    end = (index.max() + pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    cal = xcals.get_calendar("XNYS", start=start, end=end)
    dates = []
    for period, observed in pd.Series(index, index=index).groupby(index.to_period("W-FRI")):
        observed_last = observed.iloc[-1]
        sessions = cal.sessions_in_range(period.start_time.normalize(),
                                         period.end_time.normalize())
        if len(sessions) and observed_last == sessions[-1].tz_localize(None).normalize():
            dates.append(observed_last)
    return dates


def weekly_targets(index, raw, decision_days, threshold=.03):
    # Returns target applied at NEXT close; no lookahead on signal close.
    effective = pd.Series(0., index=index)
    previous = 0.
    decisions = []
    for date in decision_days:
        proposed = float(raw.loc[date])
        changed = abs(proposed - previous) >= threshold - 1e-12
        if changed:
            previous = proposed
        effective.loc[date] = previous
        decisions.append({"date": date.strftime("%Y-%m-%d"),
                          "proposed": round(proposed, 6),
                          "target": round(previous, 6), "changed": changed})
    # Only fill from confirmed decision days; shift one exchange session.
    effective.loc[:] = np.nan
    for d in decision_days:
        entry = next(z for z in decisions if z["date"] == d.strftime("%Y-%m-%d"))
        effective.loc[d] = entry["target"]
    return effective.ffill().shift(1).fillna(0), decisions


def simulate(prices, target, dca_fraction=0., core_cap=0., trade_fee=.001):
    # One fixed initial cash account, not external cash contributions.
    cash, tactical, core = 1., 0., 0.
    last_price, last_target = None, None
    equity, counts, turnover = [], 0, 0.
    for day, price in prices.items():
        if last_price is not None:
            growth = price / last_price
            tactical *= growth
            core *= growth
        if last_price is not None and dca_fraction and cash > 0 and core < core_cap:
            add = min(dca_fraction, core_cap - core, cash / (1 + trade_fee))
            if add > 0:
                core += add
                cash -= add * (1 + trade_fee)
                turnover += add
                counts += 1
        total = cash + tactical + core
        t = float(target.loc[day])
        if last_target is None or abs(t - last_target) > 1e-12:
            delta = total * t - tactical
            if delta > 0:
                amount = min(delta, cash / (1 + trade_fee))
                cash -= amount * (1 + trade_fee)
                tactical += amount
            else:
                amount = min(-delta, tactical)
                tactical -= amount
                cash += amount * (1 - trade_fee)
            if amount > 1e-12:
                turnover += amount
                counts += 1
        last_target, last_price = t, price
        equity.append(cash + tactical + core)
    return pd.Series(equity, index=prices.index), counts, turnover


def metrics(s, trades, turnover):
    years = (s.index[-1] - s.index[0]).days / 365.25
    cagr = (s.iloc[-1] / s.iloc[0]) ** (1 / years) - 1
    maxdd = float((s / s.cummax() - 1).min())
    return {"cagr_pct": round(100*cagr, 3),
            "max_drawdown_pct": round(100*maxdd, 3),
            "calmar": round(cagr / abs(maxdd), 3) if maxdd else None,
            "end_multiple": round(float(s.iloc[-1]), 4),
            "trades": trades, "turnover_initial_capital": round(turnover, 3)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default="")
    parser.add_argument("--output", default="results/ndx_dual_track.json")
    parser.add_argument("--base-rmb", type=float, default=115000)
    parser.add_argument("--dca-rmb-per-session", type=float, default=300)
    parser.add_argument("--core-cap-pct", type=float, default=20)
    parser.add_argument("--cost-bps", type=float, default=10)
    args = parser.parse_args()
    if not 0 <= args.core_cap_pct <= 100 or args.base_rmb <= 0 or args.cost_bps < 0:
        raise ValueError("Invalid configuration")
    prices, src = load_prices(args.csv)
    prices = prices.loc["1999-01-01":"2025-12-31"]
    if prices.index.min() > pd.Timestamp("2000-01-01") or prices.index.max() < pd.Timestamp("2025-12-01"):
        raise ValueError("Need historical NDX from ~1999 through end of 2025")
    f, enhanced = signals(prices)
    decision_days = decision_dates(prices.index)
    ft, fd = weekly_targets(prices.index, f, decision_days)
    et, ed = weekly_targets(prices.index, enhanced, decision_days)
    out = {"status": "INDEX_PROXY_ONLY_NOT_LIVE_TRADING",
           "source": src, "last_date": str(prices.index[-1].date()),
           "assumptions": {"initial_capital_rmb": args.base_rmb,
                           "cash_funded_dca_rmb_per_session": args.dca_rmb_per_session,
                           "core_cap_pct": args.core_cap_pct,
                           "cost_bps_per_transaction": args.cost_bps,
                           "signal": "weekly final XNYS session close",
                           "execution": "next observed close, proxy only",
                           "enhancement": "F target x0.5 if close<MA20 or MA20<MA50",
                           "excluded": "ETF historical premium, bid/ask, FX, QDII quota, active-fund NAV, fund costs, taxes"},
           "periods": {}}
    for label, start, end in (("in_sample", "2001-01-01", "2017-12-31"),
                              ("out_of_sample", "2018-01-01", "2025-12-31"),
                              ("full", "2001-01-01", "2025-12-31")):
        subset = prices.loc[start:end]
        if len(subset) < 100:
            raise ValueError("Missing period " + label)
        experiments = {
            "F_original": (ft, 0., 0.),
            "F_enhanced_20_50": (et, 0., 0.),
            "dual_track_index_proxy": (et * (1-args.core_cap_pct/100),
                                       args.dca_rmb_per_session / args.base_rmb,
                                       args.core_cap_pct/100)}
        rows = {}
        for name, (target, daily, cap) in experiments.items():
            eq, n, turn = simulate(subset, target.loc[subset.index], daily, cap,
                                   args.cost_bps/10000)
            rows[name] = metrics(eq, n, turn)
        out["periods"][label] = {"start": str(subset.index[0].date()),
                                  "end": str(subset.index[-1].date()),
                                  "sessions": len(subset), "results": rows}
    out["note"] = "Research only. Historical period split is NOT a genuine untuned holdout if parameter choices were influenced by 2018-2025 outcomes."
    dest = Path(args.output)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
