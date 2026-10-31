# -*- coding: utf-8 -*-
"""
Global Capital Flow Intelligence (GCFI)
Phase 2: Advanced Capital Flow Analytics, Correlation & Rotation Engine
"""

import json
import logging
import sqlite3
import numpy as np
import pandas as pd
from typing import Dict, Any, List, Tuple
from datetime import datetime

from gcfi.gcfi_config import DB_PATH, ETF_UNIVERSE
from gcfi.db import get_connection, save_correlations, save_rotation_summary

logger = logging.getLogger("GCFI.FlowEngine")

# Key asset pairs for cross-asset correlation analysis
CROSS_PAIRS = [
    ("SPY", "TLT", "Equity vs Long Treasury (Stock/Bond Decoupling)"),
    ("SPY", "GLD", "Equity vs Gold (Risk-On vs Safe-Haven/Hedge)"),
    ("SPY", "HYG", "Equity vs High Yield Credit (Equity/Credit Alignment)"),
    ("TLT", "USO", "Long Treasury vs Oil (Duration vs Inflation Driver)"),
    ("GLD", "UUP", "Gold vs Dollar (Anti-Fiat / Currency Hedge)"),
    ("QQQ", "SPY", "Tech vs Broad Market (Growth vs Value Bias)"),
    ("SPY", "SGOV", "Equity vs Ultra-Short Cash (Risk Asset vs Cash Hoarding)"),
]

CORR_WINDOWS = [5, 20, 60]

class CapitalFlowEngine:
    """Computes advanced flow metrics, cross-asset correlations, and capital rotation networks."""

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path

    def load_time_series(self) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Load historical daily returns and net flows for all ETFs from SQLite DB.
        Returns (returns_pivot, flows_pivot).
        """
        conn = get_connection(self.db_path)
        df = pd.read_sql_query(
            "SELECT date, ticker, close, return_1d, net_flow, net_flow_5d, flow_acceleration, aum FROM daily_etf_flows ORDER BY date ASC",
            conn,
        )
        conn.close()

        if df.empty:
            return pd.DataFrame(), pd.DataFrame()

        returns_pivot = df.pivot(index="date", columns="ticker", values="return_1d")
        flows_pivot = df.pivot(index="date", columns="ticker", values="net_flow")
        return returns_pivot, flows_pivot

    def compute_cross_asset_correlations(self, returns_pivot: pd.DataFrame, flows_pivot: pd.DataFrame) -> List[Dict[str, Any]]:
        """
        Compute rolling return and flow correlations across key asset pairs over 5D, 20D, and 60D windows.
        """
        logger.info("Computing cross-asset rolling correlations...")
        if returns_pivot.empty or flows_pivot.empty:
            return []

        latest_date = returns_pivot.index[-1]
        correlation_records = []

        for t1, t2, desc in CROSS_PAIRS:
            if t1 not in returns_pivot.columns or t2 not in returns_pivot.columns:
                continue

            r1 = returns_pivot[t1]
            r2 = returns_pivot[t2]
            f1 = flows_pivot[t1]
            f2 = flows_pivot[t2]

            pair_name = f"{t1}_{t2}"

            for w in CORR_WINDOWS:
                if len(returns_pivot) >= w:
                    ret_corr = r1.iloc[-w:].corr(r2.iloc[-w:])
                    flow_corr = f1.iloc[-w:].corr(f2.iloc[-w:])

                    ret_corr_val = round(float(ret_corr), 4) if not np.isnan(ret_corr) else 0.0
                    flow_corr_val = round(float(flow_corr), 4) if not np.isnan(flow_corr) else 0.0

                    correlation_records.append({
                        "date": latest_date,
                        "pair": pair_name,
                        "window": w,
                        "return_corr": ret_corr_val,
                        "flow_corr": flow_corr_val,
                        "description": desc,
                    })

        save_correlations(correlation_records, self.db_path)
        logger.info(f"Saved {len(correlation_records)} correlation metrics for date {latest_date}.")
        return correlation_records

    def analyze_flow_velocity_and_spikes(self) -> List[Dict[str, Any]]:
        """
        Compute Flow Velocity (5D flow / AUM %) and 20D Flow Z-Score to detect institutional block flow spikes.
        """
        conn = get_connection(self.db_path)
        latest_date_row = conn.execute("SELECT MAX(date) as max_date FROM daily_etf_flows").fetchone()
        latest_date = latest_date_row["max_date"]

        df = pd.read_sql_query(
            "SELECT date, ticker, close, aum, net_flow, net_flow_5d, flow_acceleration FROM daily_etf_flows ORDER BY date ASC",
            conn,
        )
        conn.close()

        velocity_records = []
        for ticker in ETF_UNIVERSE.keys():
            sub = df[df["ticker"] == ticker]
            if len(sub) < 5:
                continue

            latest_row = sub[sub["date"] == latest_date]
            if latest_row.empty:
                continue
            latest = latest_row.iloc[0]

            aum = latest["aum"] or 1.0
            nf5 = latest["net_flow_5d"] or 0.0
            fa = latest["flow_acceleration"] or 0.0

            # Flow Velocity (% of AUM per 5 days)
            velocity_pct = round((nf5 / aum) * 100.0, 3)

            # 20-day Flow Z-Score
            flow_20d_series = sub["net_flow"].iloc[-20:]
            mean_20d = flow_20d_series.mean()
            std_20d = flow_20d_series.std()
            z_score = round(float((latest["net_flow"] - mean_20d) / (std_20d + 1e-6)), 2)

            is_spike = abs(z_score) >= 2.0
            spike_type = "SURGE_INFLOW" if z_score >= 2.0 else ("SURGE_OUTFLOW" if z_score <= -2.0 else "NORMAL")

            velocity_records.append({
                "ticker": ticker,
                "asset_class": ETF_UNIVERSE[ticker]["asset_class"],
                "aum": aum,
                "nf5": nf5,
                "velocity_pct": velocity_pct,
                "z_score_20d": z_score,
                "is_spike": is_spike,
                "spike_type": spike_type,
            })

        return velocity_records

    def track_capital_rotation_network(self) -> Dict[str, Any]:
        """
        Track Source-to-Destination capital rotation:
        Maps which asset classes capital is exiting (Sources) and where it is being absorbed (Destinations).
        """
        conn = get_connection(self.db_path)
        latest_date_row = conn.execute("SELECT MAX(date) as max_date FROM daily_etf_flows").fetchone()
        latest_date = latest_date_row["max_date"]

        rows = conn.execute("""
            SELECT f.ticker, u.asset_class, f.net_flow_5d, f.net_flow_20d, f.aum
            FROM daily_etf_flows f
            JOIN asset_universe u ON f.ticker = u.ticker
            WHERE f.date = ?
        """, (latest_date,)).fetchall()
        conn.close()

        # Group by asset class
        ac_map = {}
        for r in rows:
            ac = r["asset_class"]
            if ac not in ac_map:
                ac_map[ac] = {"flow_5d": 0.0, "tickers": [], "aum_total": 0.0}
            ac_map[ac]["flow_5d"] += r["net_flow_5d"] or 0.0
            ac_map[ac]["tickers"].append(r["ticker"])
            ac_map[ac]["aum_total"] += r["aum"] or 0.0

        # High-level asset aggregations
        equity_flow = sum(v["flow_5d"] for k, v in ac_map.items() if "EQUITY" in k)
        bond_flow = sum(v["flow_5d"] for k, v in ac_map.items() if "TREASURY" in k or "CORP" in k)
        cash_flow = ac_map.get("SHORT_TERM_CASH", {}).get("flow_5d", 0.0)
        gold_flow = ac_map.get("PRECIOUS_METALS", {}).get("flow_5d", 0.0)
        usd_flow = ac_map.get("CURRENCY_USD", {}).get("flow_5d", 0.0)
        hyg_flow = ac_map.get("CORP_HY", {}).get("flow_5d", 0.0)
        tlt_flow = ac_map.get("US_TREASURY_LONG", {}).get("flow_5d", 0.0)

        # Relative Ratios
        equity_vs_bond_ratio = round(equity_flow / (abs(bond_flow) + 1e-3), 2)
        risk_flow = equity_flow + hyg_flow
        risk_vs_cash_ratio = round(risk_flow / (abs(cash_flow) + 1e-3), 2)
        hy_vs_tlt_ratio = round(hyg_flow / (abs(tlt_flow) + 1e-3), 2)
        gold_vs_dollar_ratio = round(gold_flow / (abs(usd_flow) + 1e-3), 2)

        # Identify Sources (NF5 < 0) and Destinations (NF5 > 0)
        sources = {k: v["flow_5d"] for k, v in ac_map.items() if v["flow_5d"] < -1e6}
        destinations = {k: v["flow_5d"] for k, v in ac_map.items() if v["flow_5d"] > 1e6}

        total_outflow = sum(abs(v) for v in sources.values())
        total_inflow = sum(v for v in destinations.values())

        # Construct transition probability mapping
        transition_map = []
        if total_outflow > 0 and total_inflow > 0:
            for src_ac, out_amt in sources.items():
                src_abs = abs(out_amt)
                for dst_ac, in_amt in destinations.items():
                    share_of_destination = in_amt / total_inflow
                    estimated_rotated_flow = src_abs * share_of_destination
                    transition_map.append({
                        "from_asset": src_ac,
                        "to_asset": dst_ac,
                        "outflow_amount_m": round(src_abs / 1e6, 1),
                        "absorption_share_pct": round(share_of_destination * 100.0, 1),
                        "estimated_flow_m": round(estimated_rotated_flow / 1e6, 1),
                    })

        rotation_record = {
            "date": latest_date,
            "equity_total_flow_5d": round(equity_flow, 2),
            "bond_total_flow_5d": round(bond_flow, 2),
            "equity_vs_bond_ratio": equity_vs_bond_ratio,
            "cash_flight_flow_5d": round(cash_flow, 2),
            "risk_vs_cash_ratio": risk_vs_cash_ratio,
            "hy_vs_tlt_flow_ratio": hy_vs_tlt_ratio,
            "gold_vs_dollar_flow_ratio": gold_vs_dollar_ratio,
            "source_assets": json.dumps(sources),
            "destination_assets": json.dumps(destinations),
            "flow_transition_map": json.dumps(transition_map),
        }

        save_rotation_summary(rotation_record, self.db_path)

        return {
            "date": latest_date,
            "equity_flow_5d_m": round(equity_flow / 1e6, 1),
            "bond_flow_5d_m": round(bond_flow / 1e6, 1),
            "equity_vs_bond_ratio": equity_vs_bond_ratio,
            "cash_flow_5d_m": round(cash_flow / 1e6, 1),
            "risk_vs_cash_ratio": risk_vs_cash_ratio,
            "hy_vs_tlt_ratio": hy_vs_tlt_ratio,
            "sources": sources,
            "destinations": destinations,
            "transitions": transition_map,
        }
