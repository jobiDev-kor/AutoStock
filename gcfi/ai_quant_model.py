# -*- coding: utf-8 -*-
"""
Global Capital Flow Intelligence (GCFI)
Phase 4: AI Quant Directional Model (RFF, Ridge, Gradient Boosting)
Predicts US Index Futures (ES / NQ) 1-Day and 5-Day Forward Momentum
"""

import logging
import sqlite3
import numpy as np
import pandas as pd
from typing import Dict, Any, List, Tuple
from datetime import datetime

from sklearn.kernel_approximation import RBFSampler
from sklearn.linear_model import RidgeClassifier, Ridge
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import accuracy_score

from gcfi.gcfi_config import DB_PATH, ETF_UNIVERSE
from gcfi.db import get_connection

logger = logging.getLogger("GCFI.AIQuant")

class AIQuantPredictor:
    """Predicts US Index Futures directional probability using capital flows & macro regime features."""

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self.feature_names = []

    def build_dataset(self) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """
        Extract and align all capital flow, macro, and regime features into a time-series matrix.
        Returns (feature_df, target_es_df, target_nq_df).
        """
        conn = get_connection(self.db_path)

        # 1. Daily ETF Flows pivot
        flows_df = pd.read_sql_query(
            "SELECT date, ticker, close, return_1d, net_flow_5d, flow_acceleration, aum FROM daily_etf_flows ORDER BY date ASC",
            conn,
        )

        # 2. Daily Macro Metrics
        macro_df = pd.read_sql_query(
            "SELECT date, us10y_yield, vix_index, dxy_index, wti_oil, hy_spread_proxy, curve_slope_proxy FROM daily_macro_metrics ORDER BY date ASC",
            conn,
        )

        # 3. Market Regimes
        regime_df = pd.read_sql_query(
            "SELECT date, risk_score, liquidity_score, credit_score FROM market_regimes ORDER BY date ASC",
            conn,
        )
        conn.close()

        if flows_df.empty or macro_df.empty:
            return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

        # Pivot ETF metrics
        p_nf5 = flows_df.pivot(index="date", columns="ticker", values="net_flow_5d")
        p_fa = flows_df.pivot(index="date", columns="ticker", values="flow_acceleration")
        p_ret = flows_df.pivot(index="date", columns="ticker", values="return_1d")
        p_close = flows_df.pivot(index="date", columns="ticker", values="close")

        # Feature Assembly
        feature_dict = {}

        # ETF Flow features (Key drivers)
        for t in ["SPY", "QQQ", "TLT", "HYG", "GLD", "SGOV", "USO", "UUP", "IBIT"]:
            if t in p_nf5.columns:
                feature_dict[f"nf5_{t}"] = p_nf5[t] / 1e8  # Normalized in $100M units
            if t in p_fa.columns:
                feature_dict[f"fa_{t}"] = p_fa[t] / 1e7    # Normalized in $10M/d units
            if t in p_ret.columns:
                feature_dict[f"ret1d_{t}"] = p_ret[t]

        # Macro features
        macro_df.set_index("date", inplace=True)
        for col in ["us10y_yield", "vix_index", "dxy_index", "wti_oil", "hy_spread_proxy", "curve_slope_proxy"]:
            if col in macro_df.columns:
                feature_dict[col] = macro_df[col]

        # Regime scores
        if not regime_df.empty:
            regime_df.set_index("date", inplace=True)
            for col in ["risk_score", "liquidity_score", "credit_score"]:
                if col in regime_df.columns:
                    feature_dict[col] = regime_df[col]

        feat_df = pd.DataFrame(feature_dict).dropna()

        # Target Construction: Forward 1D and 5D return for SPY (proxy for ES) and QQQ (proxy for NQ)
        spy_close = p_close["SPY"].reindex(feat_df.index).ffill()
        qqq_close = p_close["QQQ"].reindex(feat_df.index).ffill()

        es_targets = pd.DataFrame(index=feat_df.index)
        es_targets["fwd_ret_1d"] = spy_close.pct_change(1).shift(-1)
        es_targets["fwd_ret_5d"] = spy_close.pct_change(5).shift(-5)
        es_targets["target_1d"] = (es_targets["fwd_ret_1d"] > 0).astype(int)
        es_targets["target_5d"] = (es_targets["fwd_ret_5d"] > 0).astype(int)

        nq_targets = pd.DataFrame(index=feat_df.index)
        nq_targets["fwd_ret_1d"] = qqq_close.pct_change(1).shift(-1)
        nq_targets["fwd_ret_5d"] = qqq_close.pct_change(5).shift(-5)
        nq_targets["target_1d"] = (nq_targets["fwd_ret_1d"] > 0).astype(int)
        nq_targets["target_5d"] = (nq_targets["fwd_ret_5d"] > 0).astype(int)

        self.feature_names = list(feat_df.columns)
        return feat_df, es_targets, nq_targets

    def train_and_predict(self) -> Dict[str, Any]:
        """
        Train multi-model ensemble (Ridge, RFF Kernel, Gradient Boosting)
        and generate predictions for next 1D and 5D horizons.
        """
        feat_df, es_targets, nq_targets = self.build_dataset()
        if len(feat_df) < 30:
            return {"error": "Insufficient historical data for model training"}

        latest_date = feat_df.index[-1]
        X_latest = feat_df.iloc[[-1]]  # Feature vector for tomorrow

        results = {
            "date": latest_date,
            "sample_size": len(feat_df),
            "features_count": len(self.feature_names),
            "predictions": {},
            "top_drivers": [],
        }

        scaler = StandardScaler()

        # Evaluate both ES and NQ across 1D and 5D horizons
        for symbol, targets_df in [("ES", es_targets), ("NQ", nq_targets)]:
            results["predictions"][symbol] = {}

            for horizon in ["1d", "5d"]:
                target_col = f"target_{horizon}"
                valid_mask = targets_df[target_col].notna()

                X_train_raw = feat_df.loc[valid_mask]
                y_train = targets_df.loc[valid_mask, target_col].values

                if len(X_train_raw) < 25:
                    continue

                X_train = scaler.fit_transform(X_train_raw)
                X_pred = scaler.transform(X_latest)

                # 1. Model 1: Ridge Classifier
                ridge = RidgeClassifier(alpha=1.0)
                ridge.fit(X_train, y_train)
                ridge_pred = int(ridge.predict(X_pred)[0])
                ridge_acc = round(accuracy_score(y_train, ridge.predict(X_train)), 3)

                # 2. Model 2: Random Fourier Features (RFF) Non-linear Kernel Ridge
                rff = RBFSampler(n_components=80, gamma=0.1, random_state=42)
                X_train_rff = rff.fit_transform(X_train)
                X_pred_rff = rff.transform(X_pred)

                rff_model = RidgeClassifier(alpha=1.5)
                rff_model.fit(X_train_rff, y_train)
                rff_pred = int(rff_model.predict(X_pred_rff)[0])
                rff_acc = round(accuracy_score(y_train, rff_model.predict(X_train_rff)), 3)

                # 3. Model 3: HistGradientBoosting
                gb = HistGradientBoostingClassifier(max_iter=50, random_state=42, min_samples_leaf=5)
                gb.fit(X_train, y_train)
                gb_proba = float(gb.predict_proba(X_pred)[0][1])
                gb_pred = int(gb_proba >= 0.5)
                gb_acc = round(accuracy_score(y_train, gb.predict(X_train)), 3)

                # Ensemble Consensus (Vote & Probability)
                ensemble_votes = [ridge_pred, rff_pred, gb_pred]
                bullish_votes = sum(ensemble_votes)
                ensemble_direction = "BULLISH (UP)" if bullish_votes >= 2 else "BEARISH (DOWN)"
                consensus_confidence = round(0.50 + (abs(bullish_votes - 1.5) / 1.5) * 0.40, 2)

                results["predictions"][symbol][horizon] = {
                    "direction": ensemble_direction,
                    "confidence": consensus_confidence,
                    "bullish_votes": f"{bullish_votes}/3",
                    "model_outputs": {
                        "Ridge (Linear L2)": {"pred": "UP" if ridge_pred else "DOWN", "train_acc": ridge_acc},
                        "RFF Kernel (Non-linear)": {"pred": "UP" if rff_pred else "DOWN", "train_acc": rff_acc},
                        "Gradient Boosting (Tree)": {"pred": "UP" if gb_pred else "DOWN", "proba": round(gb_proba, 2), "train_acc": gb_acc},
                    }
                }

        # Feature Importance from Linear Ridge weights on ES 5D
        if "target_5d" in es_targets.columns:
            valid_mask = es_targets["target_5d"].notna()
            X_tr = scaler.fit_transform(feat_df.loc[valid_mask])
            y_tr = es_targets.loc[valid_mask, "target_5d"].values
            r_imp = RidgeClassifier(alpha=1.0).fit(X_tr, y_tr)
            coefs = r_imp.coef_.ravel()
            ranked_indices = np.argsort(np.abs(coefs))[::-1]
            top_drivers = []
            for idx in ranked_indices[:6]:
                top_drivers.append({
                    "feature": self.feature_names[idx],
                    "weight": round(float(coefs[idx]), 3),
                    "impact": "POSITIVE (Bullish Driver)" if coefs[idx] > 0 else "NEGATIVE (Bearish Driver)"
                })
            results["top_drivers"] = top_drivers

        # Persist predictions to database for fast query by Telegram bot
        import json
        from gcfi.db import save_ai_predictions
        db_records = []
        drivers_json = json.dumps(results.get("top_drivers", []))
        for sym, horiz_dict in results.get("predictions", {}).items():
            for horiz, pdata in horiz_dict.items():
                db_records.append({
                    "date": latest_date,
                    "symbol": sym,
                    "horizon": horiz,
                    "direction": pdata["direction"],
                    "confidence": pdata["confidence"],
                    "bullish_votes": pdata["bullish_votes"],
                    "top_drivers_json": drivers_json,
                })
        if db_records:
            save_ai_predictions(db_records, self.db_path)

        return results
