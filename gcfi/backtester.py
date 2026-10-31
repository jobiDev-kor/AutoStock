# -*- coding: utf-8 -*-
"""
Global Capital Flow Intelligence (GCFI)
Phase 5: Walk-Forward Backtesting Engine & Performance Evaluation
Simulates directional trading of ES & NQ based on capital flow signals.
"""

import logging
import sqlite3
import numpy as np
import pandas as pd
from typing import Dict, Any, List, Tuple
from datetime import datetime

from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import RidgeClassifier
from sklearn.kernel_approximation import RBFSampler

from gcfi.gcfi_config import DB_PATH
from gcfi.ai_quant_model import AIQuantPredictor
from gcfi.db import get_connection

logger = logging.getLogger("GCFI.Backtest")

class WalkForwardBacktester:
    """Evaluates predictive accuracy and Sharpe/Drawdown of capital flow signals using walk-forward testing."""

    def __init__(self, db_path=DB_PATH, min_train_window: int = 40):
        self.db_path = db_path
        self.min_train_window = min_train_window

    def run_walk_forward(self, symbol: str = "NQ") -> Dict[str, Any]:
        """
        Execute expanding-window (walk-forward) backtest for symbol ('ES' or 'NQ').
        Prevents look-ahead bias by retraining at each step t on data prior to t.
        """
        predictor = AIQuantPredictor(self.db_path)
        feat_df, es_targets, nq_targets = predictor.build_dataset()

        targets_df = es_targets if symbol == "ES" else nq_targets
        valid_df = feat_df.join(targets_df[["fwd_ret_1d", "target_1d"]]).dropna()

        if len(valid_df) <= self.min_train_window + 5:
            return {"error": "Not enough data for walk-forward validation"}

        signals = []
        actual_returns = []
        dates = []

        scaler = StandardScaler()

        # Walk-forward simulation
        for i in range(self.min_train_window, len(valid_df)):
            train_sub = valid_df.iloc[:i]
            test_row = valid_df.iloc[[i]]

            X_train = scaler.fit_transform(train_sub[predictor.feature_names])
            y_train = train_sub["target_1d"].values

            X_test = scaler.transform(test_row[predictor.feature_names])

            # RFF Kernel Model
            rff = RBFSampler(n_components=60, gamma=0.1, random_state=42)
            X_train_rff = rff.fit_transform(X_train)
            X_test_rff = rff.transform(X_test)

            model = RidgeClassifier(alpha=1.0)
            model.fit(X_train_rff, y_train)

            pred_dir = int(model.predict(X_test_rff)[0])  # 1 (Long) or 0 (Cash/Neutral)
            act_ret = float(test_row["fwd_ret_1d"].iloc[0])
            dt = test_row.index[0]

            signals.append(pred_dir)
            actual_returns.append(act_ret)
            dates.append(dt)

        signals_arr = np.array(signals)
        returns_arr = np.array(actual_returns)

        # Strategy Daily Returns: Earn actual return when Long, 0 when Cash
        strategy_returns = signals_arr * returns_arr
        benchmark_returns = returns_arr

        # Performance Metrics
        cum_strategy = np.prod(1 + strategy_returns) - 1.0
        cum_benchmark = np.prod(1 + benchmark_returns) - 1.0

        hit_count = sum((signals_arr == 1) & (returns_arr > 0)) + sum((signals_arr == 0) & (returns_arr <= 0))
        accuracy = hit_count / len(signals_arr)

        win_rate = (
            sum((signals_arr == 1) & (returns_arr > 0)) / max(sum(signals_arr == 1), 1)
        )

        mean_ret = np.mean(strategy_returns)
        std_ret = np.std(strategy_returns) + 1e-6
        sharpe = (mean_ret / std_ret) * np.sqrt(252)

        # Max Drawdown
        cum_equity = np.cumprod(1 + strategy_returns)
        peak = np.maximum.accumulate(cum_equity)
        drawdown = (cum_equity - peak) / peak
        max_drawdown = float(np.min(drawdown))

        return {
            "symbol": symbol,
            "test_days": len(signals_arr),
            "start_date": dates[0],
            "end_date": dates[-1],
            "accuracy": round(accuracy * 100.0, 1),
            "long_win_rate": round(win_rate * 100.0, 1),
            "cum_strategy_return": round(cum_strategy * 100.0, 2),
            "cum_benchmark_return": round(cum_benchmark * 100.0, 2),
            "sharpe_ratio": round(sharpe, 2),
            "max_drawdown": round(max_drawdown * 100.0, 2),
            "long_signal_ratio": round((sum(signals_arr == 1) / len(signals_arr)) * 100.0, 1),
        }
