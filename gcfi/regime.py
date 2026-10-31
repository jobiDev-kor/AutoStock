# -*- coding: utf-8 -*-
"""
Global Capital Flow Intelligence (GCFI)
Phase 3: Market Regime Detection Engine & State Transition Analytics
"""

import logging
import sqlite3
import pandas as pd
import numpy as np
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime

from gcfi.gcfi_config import DB_PATH, ETF_UNIVERSE
from gcfi.db import get_connection, save_market_regime

logger = logging.getLogger("GCFI.Regime")

class MarketRegimeDetector:
    """Analyzes cross-asset capital flows and macro metrics to identify market regimes."""

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path

    def analyze_regime_for_date(self, target_date: str) -> Optional[Dict[str, Any]]:
        """Detect the market regime for a specific historical or current date."""
        conn = get_connection(self.db_path)

        # 1. Fetch ETF flow records for target_date
        flow_rows = conn.execute("""
            SELECT f.*, u.asset_class, u.name
            FROM daily_etf_flows f
            JOIN asset_universe u ON f.ticker = u.ticker
            WHERE f.date = ?
        """, (target_date,)).fetchall()

        # 2. Fetch Macro metrics for target_date
        macro_row = conn.execute("SELECT * FROM daily_macro_metrics WHERE date = ?", (target_date,)).fetchone()
        conn.close()

        if not flow_rows:
            return None

        flows = {r["ticker"]: dict(r) for r in flow_rows}
        macro = dict(macro_row) if macro_row else {}

        # Aggregate flows by Asset Class
        asset_class_flows = {}
        for ticker, data in flows.items():
            ac = data["asset_class"]
            if ac not in asset_class_flows:
                asset_class_flows[ac] = {
                    "net_flow_5d": 0.0,
                    "net_flow_20d": 0.0,
                    "flow_acceleration": 0.0,
                    "return_5d_avg": [],
                    "tickers": [],
                }
            asset_class_flows[ac]["net_flow_5d"] += data["net_flow_5d"] or 0.0
            asset_class_flows[ac]["net_flow_20d"] += data["net_flow_20d"] or 0.0
            asset_class_flows[ac]["flow_acceleration"] += data["flow_acceleration"] or 0.0
            if data["return_5d"] is not None:
                asset_class_flows[ac]["return_5d_avg"].append(data["return_5d"])
            asset_class_flows[ac]["tickers"].append(ticker)

        for ac, val in asset_class_flows.items():
            val["return_5d_avg"] = float(np.mean(val["return_5d_avg"])) if val["return_5d_avg"] else 0.0

        # Key Asset Class Indicators
        eq_flow_5d = asset_class_flows.get("US_EQUITY", {}).get("net_flow_5d", 0.0)
        tlt_flow_5d = flows.get("TLT", {}).get("net_flow_5d", 0.0)
        sgov_flow_5d = flows.get("SGOV", {}).get("net_flow_5d", 0.0)
        hyg_flow_5d = flows.get("HYG", {}).get("net_flow_5d", 0.0)
        lqd_flow_5d = flows.get("LQD", {}).get("net_flow_5d", 0.0)
        gld_flow_5d = flows.get("GLD", {}).get("net_flow_5d", 0.0)
        uso_flow_5d = flows.get("USO", {}).get("net_flow_5d", 0.0)
        uup_flow_5d = flows.get("UUP", {}).get("net_flow_5d", 0.0)

        # Returns
        spy_ret_5d = flows.get("SPY", {}).get("return_5d", 0.0)
        qqq_ret_5d = flows.get("QQQ", {}).get("return_5d", 0.0)
        tlt_ret_5d = flows.get("TLT", {}).get("return_5d", 0.0)
        hyg_ret_5d = flows.get("HYG", {}).get("return_5d", 0.0)

        vix = macro.get("vix_index", 18.0)
        us10y = macro.get("us10y_yield", 4.2)
        hy_proxy = macro.get("hy_spread_proxy", 0.9)

        # -------------------------------------------------------------
        # 1. Compute Composite Scores (-100 to +100)
        # -------------------------------------------------------------
        risk_components = [
            np.clip(spy_ret_5d * 1000, -40, 40),
            np.clip((eq_flow_5d - (tlt_flow_5d + sgov_flow_5d)) / 1e7, -30, 30),
            np.clip((20.0 - vix) * 2.0, -20, 20),
            np.clip(hyg_ret_5d * 500, -10, 10),
        ]
        risk_score = round(float(np.clip(sum(risk_components), -100.0, 100.0)), 1)

        liq_components = [
            np.clip((eq_flow_5d + tlt_flow_5d) / 1e7, -40, 40),
            np.clip(-sgov_flow_5d / 1e7, -30, 30),
            np.clip(-uup_flow_5d / 5e6, -20, 20),
            np.clip((22.0 - vix) * 1.0, -10, 10),
        ]
        liquidity_score = round(float(np.clip(sum(liq_components), -100.0, 100.0)), 1)

        credit_components = [
            np.clip((hyg_ret_5d - tlt_ret_5d) * 1500, -50, 50),
            np.clip((hyg_flow_5d - lqd_flow_5d) / 5e6, -30, 30),
            np.clip((hy_proxy - 0.8) * 50, -20, 20),
        ]
        credit_score = round(float(np.clip(sum(credit_components), -100.0, 100.0)), 1)

        # Leading and Lagging Asset Classes
        sorted_assets = sorted(
            asset_class_flows.items(),
            key=lambda x: x[1]["flow_acceleration"],
            reverse=True,
        )
        leading_asset = sorted_assets[0][0] if sorted_assets else "UNKNOWN"
        lagging_asset = sorted_assets[-1][0] if sorted_assets else "UNKNOWN"

        # -------------------------------------------------------------
        # 2. 6 Core Regime Classification Rules
        # -------------------------------------------------------------
        reasons = []

        if credit_score < -35 and hyg_flow_5d < 0 and hyg_ret_5d < -0.015:
            regime = "Credit Stress"
            reasons.append(f"HYG underperforming TLT with credit score {credit_score}; credit spread widening.")
        elif risk_score > 30 and eq_flow_5d > 0 and vix < 20.0:
            regime = "Risk-On"
            reasons.append(f"Strong equity inflows (5D: ${eq_flow_5d/1e6:.1f}M), low VIX ({vix:.1f}), risk score {risk_score}.")
        elif risk_score < -30 and (tlt_flow_5d > 0 or sgov_flow_5d > 0 or gld_flow_5d > 0):
            regime = "Risk-Off"
            reasons.append(f"Equities seeing outflows, safe havens (TLT/SGOV/GLD) absorbing capital, risk score {risk_score}.")
        elif liquidity_score > 25 and eq_flow_5d > 0 and tlt_flow_5d > 0:
            regime = "Liquidity Expansion"
            reasons.append(f"System-wide capital influx across equities & debt; liquidity score {liquidity_score}.")
        elif liquidity_score < -25 and sgov_flow_5d > 0 and eq_flow_5d < 0:
            regime = "Liquidity Contraction"
            reasons.append(f"Flight to cash/SGOV; broad capital drainage from risk assets; liquidity score {liquidity_score}.")
        elif uso_flow_5d > 0 and gld_flow_5d > 0 and tlt_flow_5d < 0 and us10y > 4.3:
            regime = "Inflationary Rotation"
            reasons.append(f"Capital rotating from long Treasuries into Energy/Commodities & Gold; 10Y Yield at {us10y:.2f}%.")
        else:
            regime = "Neutral / Transitional"
            reasons.append(f"Capital rotation signals are balanced. Risk score: {risk_score}, Liquidity score: {liquidity_score}.")

        regime_result = {
            "date": target_date,
            "regime": regime,
            "risk_score": risk_score,
            "liquidity_score": liquidity_score,
            "credit_score": credit_score,
            "leading_asset": leading_asset,
            "lagging_asset": lagging_asset,
            "trigger_reasons": " | ".join(reasons),
            "asset_class_flows": asset_class_flows,
            "macro_snapshot": macro,
        }

        # Save to DB
        save_market_regime({
            "date": target_date,
            "regime": regime,
            "risk_score": risk_score,
            "liquidity_score": liquidity_score,
            "credit_score": credit_score,
            "leading_asset": leading_asset,
            "lagging_asset": lagging_asset,
            "trigger_reasons": " | ".join(reasons),
        })

        return regime_result

    def analyze_latest_regime(self) -> Dict[str, Any]:
        """Detect the market regime for the latest available date."""
        conn = get_connection(self.db_path)
        latest_date_row = conn.execute("SELECT MAX(date) as max_date FROM daily_etf_flows").fetchone()
        conn.close()
        if not latest_date_row or not latest_date_row["max_date"]:
            return {"error": "No ETF flow data available"}
        return self.analyze_regime_for_date(latest_date_row["max_date"])

    def backfill_historical_regimes(self) -> Dict[str, Any]:
        """
        Backfill regime classification for all historical dates in daily_etf_flows.
        Enables regime persistence tracking and transition event detection.
        """
        conn = get_connection(self.db_path)
        dates = [r[0] for r in conn.execute("SELECT DISTINCT date FROM daily_etf_flows ORDER BY date ASC").fetchall()]
        conn.close()

        logger.info(f"Backfilling historical market regimes across {len(dates)} dates...")
        regime_counts = {}
        for dt in dates:
            res = self.analyze_regime_for_date(dt)
            if res:
                reg = res["regime"]
                regime_counts[reg] = regime_counts.get(reg, 0) + 1

        logger.info(f"Historical regime distribution: {regime_counts}")
        return {"total_dates": len(dates), "distribution": regime_counts}

    def detect_regime_transition(self) -> Dict[str, Any]:
        """
        Detect whether a regime shift occurred between the previous day and latest date,
        and calculate how many days the current regime has persisted.
        """
        conn = get_connection(self.db_path)
        recent_rows = conn.execute("""
            SELECT date, regime, risk_score, liquidity_score, credit_score, trigger_reasons
            FROM market_regimes
            ORDER BY date DESC
            LIMIT 30
        """).fetchall()
        conn.close()

        if not recent_rows:
            return {"transition_detected": False, "current_regime": "UNKNOWN", "duration_days": 0}

        latest = dict(recent_rows[0])
        current_regime = latest["regime"]

        # Calculate duration
        duration = 0
        for r in recent_rows:
            if r["regime"] == current_regime:
                duration += 1
            else:
                break

        prev_regime = recent_rows[1]["regime"] if len(recent_rows) > 1 else None
        transition_detected = (prev_regime is not None and prev_regime != current_regime)

        # Generate Telegram alert payload if transition or high-impact regime
        alert_msg = None
        if transition_detected:
            alert_msg = (
                f"[GCFI 긴급: 글로벌 시장 국면 전환 감지]\n\n"
                f"* 이전 국면: {prev_regime}\n"
                f"* 전환 국면: {current_regime} (지속: {duration}일차)\n"
                f"* 종합 점수: 위험 {latest['risk_score']}점 | 유동성 {latest['liquidity_score']}점 | 신용 {latest['credit_score']}점\n"
                f"* 전환 사유: {latest['trigger_reasons']}\n"
                f"* 대응 전략: 나스닥/S&P500 선물 포지션 및 현물 비중 재조정 권고"
            )

        return {
            "latest_date": latest["date"],
            "current_regime": current_regime,
            "prev_regime": prev_regime,
            "transition_detected": transition_detected,
            "duration_days": duration,
            "risk_score": latest["risk_score"],
            "liquidity_score": latest["liquidity_score"],
            "credit_score": latest["credit_score"],
            "alert_msg": alert_msg,
        }
