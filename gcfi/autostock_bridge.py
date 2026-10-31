# -*- coding: utf-8 -*-
"""
Global Capital Flow Intelligence (GCFI)
Phase 5: AutoStock Safe Integration Bridge (Read-Only)

Provides a clean, non-intrusive safety check interface for AutoStock trading logic.
Does NOT modify existing AutoStock files or execute orders.
"""

import logging
import sqlite3
from typing import Dict, Any, Tuple
from gcfi.gcfi_config import DB_PATH
from gcfi.db import get_connection

logger = logging.getLogger("GCFI.Bridge")

class AutoStockGlobalShield:
    """Read-only risk assessment bridge connecting GCFI intelligence to AutoStock execution."""

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path

    def evaluate_buy_permission(self) -> Dict[str, Any]:
        """
        Evaluate whether global capital flow and market regime permit new domestic stock purchases.
        Returns:
            {
                "allow_buy": bool,
                "regime": str,
                "risk_score": float,
                "block_reason": str or None,
                "futures_bias": str,
                "notes": str
            }
        """
        try:
            conn = get_connection(self.db_path)
            # Latest Regime
            reg_row = conn.execute("SELECT * FROM market_regimes ORDER BY date DESC LIMIT 1").fetchone()
            # Latest Signals
            sig_rows = conn.execute("SELECT * FROM futures_signals ORDER BY date DESC LIMIT 2").fetchall()
            conn.close()

            if not reg_row:
                return {
                    "allow_buy": True,
                    "regime": "UNKNOWN",
                    "risk_score": 50.0,
                    "block_reason": None,
                    "notes": "No GCFI regime data found; defaulting to allow."
                }

            regime = reg_row["regime"]
            risk_score = reg_row["risk_score"]
            credit_score = reg_row["credit_score"]

            signals = {r["symbol"]: dict(r) for r in sig_rows}
            es_dir = signals.get("ES", {}).get("direction", "NEUTRAL")
            nq_dir = signals.get("NQ", {}).get("direction", "NEUTRAL")

            # Blocking Rules
            allow_buy = True
            block_reason = None

            if regime == "Credit Stress":
                allow_buy = False
                block_reason = f"글로벌 신용 스프레드 급등 및 하이일드 채권 투매 (Credit Score: {credit_score})"
            elif regime == "Risk-Off" and risk_score < -40:
                allow_buy = False
                block_reason = f"글로벌 위험자산 극단적 투매 및 안전자산 도피 (Risk Score: {risk_score})"
            elif "SELL" in es_dir and "SELL" in nq_dir:
                allow_buy = False
                block_reason = "미국 주요 선물(S&P500 & 나스닥100) 동반 급락 및 강력한 하방 수급 압력"

            return {
                "allow_buy": allow_buy,
                "regime": regime,
                "risk_score": risk_score,
                "credit_score": credit_score,
                "block_reason": block_reason,
                "es_signal": es_dir,
                "nq_signal": nq_dir,
                "notes": f"GCFI Shield Status: {'[PERMITTED]' if allow_buy else '[BLOCKED]'} | Regime: {regime}"
            }

        except Exception as e:
            logger.error(f"Error evaluating AutoStock safety bridge: {e}")
            return {
                "allow_buy": True,
                "regime": "ERROR_FALLBACK",
                "risk_score": 50.0,
                "block_reason": None,
                "notes": f"Safety bridge error fallback: {e}"
            }

def check_global_market_safety() -> Tuple[bool, str]:
    """Simple 1-line helper for external callers."""
    shield = AutoStockGlobalShield()
    res = shield.evaluate_buy_permission()
    return res["allow_buy"], res.get("block_reason") or res["notes"]
