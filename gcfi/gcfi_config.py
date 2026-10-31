# -*- coding: utf-8 -*-
"""
Global Capital Flow Intelligence (GCFI)
Configuration and Asset Universe Definitions
"""

import os
from pathlib import Path

# Paths
BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
DB_PATH = BASE_DIR / "gcfi.db"

# 15 Core ETFs across 9 Asset Classes
ETF_UNIVERSE = {
    # 1. US Equities
    "SPY": {"name": "SPDR S&P 500 ETF Trust", "asset_class": "US_EQUITY", "benchmark": True},
    "QQQ": {"name": "Invesco QQQ Trust (Nasdaq 100)", "asset_class": "US_EQUITY", "benchmark": False},
    "IWM": {"name": "iShares Russell 2000 ETF", "asset_class": "US_EQUITY", "benchmark": False},

    # 2. Global Equities
    "EFA": {"name": "iShares MSCI EAFE ETF (Developed ex-US)", "asset_class": "GLOBAL_EQUITY", "benchmark": False},
    "EEM": {"name": "iShares MSCI Emerging Markets ETF", "asset_class": "GLOBAL_EQUITY", "benchmark": False},

    # 3. US Treasuries
    "TLT": {"name": "iShares 20+ Year Treasury Bond ETF", "asset_class": "US_TREASURY_LONG", "benchmark": True},
    "IEF": {"name": "iShares 7-10 Year Treasury Bond ETF", "asset_class": "US_TREASURY_MID", "benchmark": False},
    "SHY": {"name": "iShares 1-3 Year Treasury Bond ETF", "asset_class": "US_TREASURY_SHORT", "benchmark": False},

    # 4. Short-term / Money Market / Cash
    "SGOV": {"name": "iShares 0-3 Month Treasury Bond ETF", "asset_class": "SHORT_TERM_CASH", "benchmark": True},

    # 5. Corporate Credit
    "LQD": {"name": "iShares iBoxx $ Investment Grade Corporate Bond ETF", "asset_class": "CORP_IG", "benchmark": False},
    "HYG": {"name": "iShares iBoxx $ High Yield Corporate Bond ETF", "asset_class": "CORP_HY", "benchmark": True},

    # 6. Precious Metals
    "GLD": {"name": "SPDR Gold Shares", "asset_class": "PRECIOUS_METALS", "benchmark": True},

    # 7. Commodities & Energy
    "USO": {"name": "United States Oil Fund", "asset_class": "COMMODITIES_ENERGY", "benchmark": False},

    # 8. Currencies
    "UUP": {"name": "Invesco DB US Dollar Index Bullish Fund", "asset_class": "CURRENCY_USD", "benchmark": True},

    # 9. Crypto
    "IBIT": {"name": "iShares Bitcoin Trust ETF", "asset_class": "CRYPTO_ASSETS", "benchmark": False},
}

# Macro Reference Indicators
MACRO_UNIVERSE = {
    "^TNX": {"name": "CBOE 10-Year US Treasury Yield", "metric": "US10Y_YIELD"},
    "^VIX": {"name": "CBOE Volatility Index", "metric": "VIX_VOLATILITY"},
    "DX-Y.NYB": {"name": "US Dollar Index Cash", "metric": "DXY_INDEX"},
    "CL=F": {"name": "Crude Oil WTI Futures", "metric": "WTI_CRUDE"},
}

# Lookback windows
DEFAULT_HISTORY_PERIOD = "6mo"  # 6 months daily history
FLOW_WINDOW_SHORT = 5           # 5-day flow
FLOW_WINDOW_LONG = 20           # 20-day flow
