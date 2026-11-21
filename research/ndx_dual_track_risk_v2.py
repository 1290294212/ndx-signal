#!/usr/bin/env python3
"""V2: compare cash-funded NDX index proxies, NOT investable QDII returns."""
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
from ndx_dual_track_backtest import load_prices, signals, decision_dates, weekly_targets

START = "1999-01-01"
END = "2025-12-31"


def trading_schedule(index, weekly_closes):
    """The week-ending signal becomes actionable on the next observed trading close."""
    positions = index.get_indexer(weekly_closes)
    return {index[i + 1] for i in positions if 0 <= i < len(index) - 1}


def run(prices, target, rebalance_days, mode, core_cap_pct,
        dca_rmb, initial_rmb, fee_bps):
    cash, core, tactical = 1., 0., 0.
    fee = fee_bps / 10000
    cash_dca = dca_rmb / initial_rmb
    last_price = None
    cumulative_volume = 0.
    tactical_trades = 0
    dca_trades = 0
    records = []
    high = 1.
    for day, p in prices.items():
        if last_price is not None:
            g = float(p / last_price)
            core *= g
            tactical *= g
        total = cash + core + tactical
        desired = float(target.loc[day])
        core_weight = core / total if total > 0 else 0.
        core_ceiling = core_cap_pct / 100
        # Scenario: prevent NEW long-term purchases when that sleeve already
        # consumes the overall F weight; never sell the long-term sleeve.
        eligible = (mode != "throttle" or core_weight < min(core_ceiling, desired))
        if mode != "F_only" and day != prices.index[0] and eligible and core < core_ceiling:
            value = min(cash_dca, core_ceiling - core, cash / (1 + fee))
            if value > 1e-12:
                cash -= value * (1 + fee)
                core += value
                cumulative_volume += value
                dca_trades += 1
        # Only trade tactical sleeve on the first session after the weekly close.
        if day in rebalance_days or day == prices.index[0]:
            total = cash + core + tactical
            if mode == "F_only":
                tactic_weight = desired
            elif mode == "separate":
                tactic_weight = desired * (1 - core_ceiling)
            else:
                tactic_weight = max(0., desired - core / total)
            delta = tactic_weight * total - tactical
            if delta > 0:
                value = min(delta, cash / (1 + fee))
                cash -= value * (1 + fee)
                tactical += value
            else:
                value = min(-delta, tactical)
                cash += value * (1 - fee)
                tactical -= value
            if value > 1e-12:
                cumulative_volume += value
                tactical_trades += 1
        total = cash + core + tactical
        high = max(high, total)
        records.append((day, total, core/total, tactical/total, (total/high-1)))
        last_price = p
    df = pd.DataFrame(records, columns=["date", "equity", "core_weight",
                                         "tactical_weight", "drawdown"]).set_index("date")
    annual = (df.index[-1] - df.index[0]).days / 365.25
    cagr = float((df.equity.iloc[-1] / df.equity.iloc[0]) ** (1 / annual) - 1)
    dd = float(df.drawdown.min())
    return {
        "annual_return_pct": round(cagr * 100, 3),
        "max_drawdown_pct": round(dd * 100, 3),
        "calmar": round(cagr/abs(dd), 3) if dd else None,
        "end_multiple": round(float(df.equity.iloc[-1]), 4),
        "core_end_pct": round(float(df.core_weight.iloc[-1])*100, 2),
        "core_peak_pct": round(float(df.core_weight.max())*100, 2),
        "tactical_trades": tactical_trades,
        "dca_purchases": dca_trades,
        "turnover_initial_capital": round(cumulative_volume, 3),
        "ever_below_minus_10pct": bool((df.drawdown <= -.10).any()),
        "ever_below_minus_12_5pct": bool((df.drawdown <= -.125).any()),
        "ever_below_minus_15pct": bool((df.drawdown <= -.15).any())
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="")
    ap.add_argument("--output", default="results/dual_track_risk_v2.json")
    ap.add_argument("--base-rmb", type=float, default=115000)
    ap.add_argument("--dca-rmb-per-session", type=float, default=300)
    ap.add_argument("--cost-bps", type=float, default=10)
    args = ap.parse_args()
    if args.base_rmb <= 0 or args.dca_rmb_per_session < 0 or args.cost_bps < 0:
        raise ValueError("Invalid capital/DCA/fee")
    prices, source = load_prices(args.csv)
    prices = prices.loc[START:END]
    if prices.index[0] > pd.Timestamp("2000-01-01") or prices.index[-1] < pd.Timestamp("2025-12-01"):
        raise ValueError("Missing 1999 warm-up or 2025 data")
    f, _ = signals(prices)
    weeks = decision_dates(prices.index)
    applied, _ = weekly_targets(prices.index, f, weeks)
    schedule = trading_schedule(prices.index, weeks)
    result = {
        "status": "RESEARCH_ONLY_INDEX_PROXY",
        "source": source,
        "periods": {},
        "notes": [
            "One initial cash account; DCA is funded by existing cash, not external contributions.",
            "Both long-term and tactical sleeves use NDX closing prices as a proxy; actual fund NAVs and ETF premiums are absent.",
            "A core cap is a CONTRIBUTION ceiling, not a portfolio-weight ceiling: market appreciation can exceed it.",
            "Risk alerts only; 10/12.5/15 percent are NOT proven stop-loss thresholds.",
            "The 2018-2025 period is chronologically separate but NOT untouched out-of-sample validation.",
            "Signal calculated at last weekly exchange close and used no earlier than next observed close; all trades are optimistic close proxies.",
            "No FX, QDII subscription limits, active funds, taxes, market impact, bid-ask, ETF premium history."
        ],
        "assumptions": {
            "initial_rmb": args.base_rmb,
            "dca_rmb_per_trading_session": args.dca_rmb_per_session,
            "cost_bps_each_transaction": args.cost_bps,
            "core_contribution_caps_pct": [5, 10, 15, 20],
            "modes": {
                "F_only": "100 percent allocation budget follows original F target",
                "separate": "tactical target F x (1 - core contribution cap), plus core",
                "deduct": "tactical target max(0, F target - actual core weight)",
                "throttle": "deduct plus suspend new core DCA while core weight >= min(core cap,F target)"
            }
        }
    }
    for name, a, b in (("2001_2017", "2001-01-01", "2017-12-31"),
                       ("2018_2025_not_pristine_oos", "2018-01-01", "2025-12-31"),
                       ("2001_2025", "2001-01-01", "2025-12-31")):
        p = prices.loc[a:b]
        # Starting a new experiment at a subperiod resets funding and holdings.
        # The most recent historical F decision remains known at period start.
        t = applied.loc[p.index]
        days = set(p.index).intersection(schedule)
        group = {"start": str(p.index[0].date()), "end": str(p.index[-1].date()),
                 "sessions": len(p), "scenarios": {}}
        group["scenarios"]["F_only"] = run(p,t,days,"F_only",0,0,args.base_rmb,args.cost_bps)
        for cap in (5,10,15,20):
            for mode in ("separate","deduct","throttle"):
                group["scenarios"][f"{mode}_cap_{cap}pct"] = run(
                    p,t,days,mode,cap,args.dca_rmb_per_session,args.base_rmb,args.cost_bps)
        result["periods"][name] = group
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False),encoding="utf-8")
    print(json.dumps({"status":result["status"],"source":source,
                      "full_period":result["periods"]["2001_2025"]},ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
