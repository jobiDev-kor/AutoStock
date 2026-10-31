# -*- coding: utf-8 -*-
"""
Global Capital Flow Intelligence (GCFI)
US Index Futures Directional Analysis Engine (ES / NQ)
"""

import logging
import sqlite3
import numpy as np
from typing import Dict, Any, List

from gcfi.gcfi_config import DB_PATH
from gcfi.db import get_connection, save_futures_signals

logger = logging.getLogger("GCFI.Futures")

class FuturesSignalEngine:
    """Evaluates directional momentum and flow confirmation for S&P 500 (ES) and Nasdaq 100 (NQ)."""

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path

    def generate_signals(self, regime_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Generate ES and NQ futures directional signals based on flows, returns, and regime."""
        latest_date = regime_data.get("date")
        if not latest_date:
            return []

        conn = get_connection(self.db_path)
        spy_row = conn.execute("SELECT * FROM daily_etf_flows WHERE date = ? AND ticker = 'SPY'", (latest_date,)).fetchone()
        qqq_row = conn.execute("SELECT * FROM daily_etf_flows WHERE date = ? AND ticker = 'QQQ'", (latest_date,)).fetchone()
        conn.close()

        if not spy_row or not qqq_row:
            return []

        spy = dict(spy_row)
        qqq = dict(qqq_row)

        regime = regime_data.get("regime", "Neutral")
        risk_score = regime_data.get("risk_score", 0.0)

        signals = []
        for symbol, data in [("ES", spy), ("NQ", qqq)]:
            ret_5d = data.get("return_5d") or 0.0
            ret_1d = data.get("return_1d") or 0.0
            nf5 = data.get("net_flow_5d") or 0.0
            fa = data.get("flow_acceleration") or 0.0

            # 1. Flow Support Score (-1.0 to +1.0)
            flow_score = float(np.clip(fa / 5e6, -1.0, 1.0))

            # 2. Divergence Analysis (Flow vs Price)
            divergence_note = "Normal flow-price co-movement."
            if ret_5d > 0.01 and nf5 < -1e6:
                divergence_note = "BEARISH DIVERGENCE: Price rising while institutional capital is net outflowing."
            elif ret_5d < -0.01 and nf5 > 1e6:
                divergence_note = "BULLISH DIVERGENCE: Price dropping while institutional capital is accumulating (dip-buying)."

            # 3. Directional Bias & Confidence
            composite_bias = (ret_5d * 30.0) + (flow_score * 0.4) + (risk_score / 200.0)

            if composite_bias > 0.35 and regime in ["Risk-On", "Liquidity Expansion"]:
                direction = "LONG (BUY)"
                confidence = round(min(0.50 + composite_bias * 0.3, 0.95), 2)
                regime_align = 1
            elif composite_bias < -0.35 or regime in ["Credit Stress", "Risk-Off"]:
                direction = "SHORT (SELL/DEFENSIVE)"
                confidence = round(min(0.50 + abs(composite_bias) * 0.3, 0.95), 2)
                regime_align = 1
            else:
                direction = "NEUTRAL / HOLD"
                confidence = 0.50
                regime_align = 0

            notes = f"Regime: {regime} | Ret5D: {ret_5d*100:+.2f}% | NF5: ${nf5/1e6:+.1f}M | FA: ${fa/1e6:+.1f}M/d | {divergence_note}"

            signals.append({
                "date": latest_date,
                "symbol": symbol,
                "direction": direction,
                "confidence": confidence,
                "flow_support": round(flow_score, 2),
                "regime_alignment": regime_align,
                "notes": notes,
            })

        save_futures_signals(signals, self.db_path)
        return signals
