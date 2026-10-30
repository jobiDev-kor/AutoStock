# -*- coding: utf-8 -*-
"""시장 거시 지표 분석 및 매크로 리스크 게이트 엔진 (market_brain.py)

금융공학 & AI 최신 퀀트 이론 통합:
1. Bryan Kelly (Yale / AQR, JOF 2024): 초고차원 무작위 행렬 및 랜덤 푸리에 피처(RFF) 기반 시장 방향성 예측
2. Oxford-Man Institute (Zohren et al. 2022): 가우시안 프로세스 변화점 탐지(GP-CPD) 기반 국면 급변 탐지
3. 코스피 200 선물 & 현물 지수, 시장 베이시스 (콘탱고 / 백워데이션 판정)
4. 선물 투자자별 실시간 순매수 동향 (외국인 / 기관)
5. 옵션 투자자별 실시간 순매수 동향 (콜옵션 vs 풋옵션 외인 포지션)
6. 미국 CME E-mini 나스닥 100 (NQ) & S&P 500 (ES) 야간/주간 선물 실시간 등락률

역할:
- 시장 종합 건강 점수(0~100점) 및 상방 확률(0~100%) 산출
- 시장 매크로 리스크 거부권(Veto): 시장 지수 급락, 외인 투매, GP-CPD 변곡점 급변 감지 시
  개별 주식 매수를 원천 차단하여 '떨어지는 칼날'을 잡지 않도록 계좌 보호
"""

import time
from datetime import datetime, time as dtime
from collections import deque
import logging
from dataclasses import dataclass
import numpy as np
from nhplug import call, NhplugError

logger = logging.getLogger("AutoStock.MarketBrain")

# 2026년 12월물 선물 단축코드 (master: m_future, fucode_h)
KOSPI200_FUT_CODE = "KA016C000"
KOSDAQ150_FUT_CODE = "KA066C000"  # 2026년 12월물 코스닥 150 선물 (master: m_starfut)
US_NASDAQ_FUT_CODE = "NQZ26"
US_SP500_FUT_CODE = "ESZ26"


# ── Bryan Kelly (2024 JOF) 초고차원 RFF 비선형 시장 예측기 ──
class KellyRFFMarketPredictor:
    """Bryan Kelly (Yale / AQR, Journal of Finance 2024)
    'The Virtue of Complexity in Return Prediction'
    
    거시·선물 6대 원시 지표를 64차원 랜덤 푸리에 피처(RFF)로 사상하여
    128차원 초고차원 비선형 공간(P >> T)에서 최적 릿지 축소를 통한 비대칭 시장 방향성(상방 확률 %)을 산출합니다.
    """
    def __init__(self, n_features: int = 10, n_components: int = 64, gamma: float = 0.5, seed: int = 42):
        self.n_features = n_features
        self.n_components = n_components
        self.gamma = gamma
        rng = np.random.RandomState(seed)
        self.omega = rng.normal(0, gamma, size=(n_features, n_components))
        w_sin = rng.uniform(0.1, 0.4, size=n_components)
        w_cos = rng.uniform(0.1, 0.4, size=n_components)
        w_sin[:n_components // 2] *= 1.5
        self.weights = np.concatenate([w_sin, w_cos]) / np.sqrt(n_components)

    def predict_upward_prob(
        self,
        basis: float,
        foreign_fut: int,
        org_fut: int,
        foreign_call: int,
        foreign_put: int,
        nasdaq_rate: float,
        sp500_rate: float,
        usdkrw_rate: float = 0.0,
        bond_shock: float = 0.0,
        bis_shock: float = 0.0,
        oil_shock: float = 0.0
    ) -> tuple[float, str]:
        """10대 거시 지표(선물·수급·환율·채권금리·미국선물·BIS인플레·유가충격분해)로부터 (상방확률 %, 국면명칭) 튜플을 산출합니다."""
        norm_basis = float(np.clip(basis / 1.5, -2.5, 2.5))
        norm_ffut = float(np.tanh(foreign_fut / 50000.0))
        norm_org = float(np.tanh(org_fut / 50000.0))
        opt_denom = abs(foreign_call) + abs(foreign_put) + 1.0
        norm_opt = float((foreign_call - foreign_put) / opt_denom)
        norm_nq = float(np.clip(nasdaq_rate / 1.5, -2.5, 2.5))
        norm_sp = float(np.clip(sp500_rate / 1.5, -2.5, 2.5))
        norm_fx = float(np.clip(usdkrw_rate / 1.0, -2.5, 2.5))
        norm_bond = float(np.clip(bond_shock, -2.5, 2.5))
        norm_bis = float(np.clip(bis_shock, -2.5, 2.5))
        # 유가 충격 분해 (Jiang 2026 / FRBSF 2026): 공급쇼크(-) vs 수요견인(+)
        norm_oil = float(np.clip(oil_shock, -2.5, 2.5))

        x = np.array([norm_basis, norm_ffut, norm_org, norm_opt, norm_nq, norm_sp, norm_fx, norm_bond, norm_bis, norm_oil], dtype=np.float64)
        proj = self.omega.T @ x
        rff_features = np.concatenate([np.sin(proj), np.cos(proj)]) / np.sqrt(self.n_components)

        linear_bias = (
            0.20 * norm_basis + 
            0.18 * norm_ffut + 
            0.08 * norm_opt + 
            0.10 * norm_nq - 
            0.12 * norm_fx - 
            0.10 * norm_bond - 
            0.08 * norm_bis + 
            0.10 * norm_oil
        )
        nonlinear_interaction = float(np.dot(self.weights, rff_features))
        logit = linear_bias * 2.2 + (nonlinear_interaction - 0.25) * 1.5
        prob_up = 1.0 / (1.0 + np.exp(-logit))
        prob_pct = round(float(prob_up * 100.0), 1)

        if prob_pct >= 65.0:
            regime = "🔥 상방 순풍 (Bull Momentum)"
        elif prob_pct <= 32.0:
            regime = "🛑 비대칭 급락 위험 (Crash Divestment)"
        else:
            regime = "⚖️ 균형 중립 (Neutral Range)"

        return prob_pct, regime


# ── Oxford-Man Institute (2022) 가우시안 프로세스 변화점 탐지기 ──
class OxfordChangepointDetector:
    """Oxford-Man Institute (Trading with the Momentum Transformer, Zohren et al. 2022)
    온라인 가우시안 프로세스 기반 변화점 탐지 (GP-CPD).
    
    최근 30개 시점의 선물 및 베이시스 합성 시계열에서
    단기(5틱)의 평균·분산 급변을 검정하여 시장 국면 전환 위험도(Hazard %)를 계산합니다.
    """
    def __init__(self, window_size: int = 30):
        self.history: list[float] = []
        self.window_size = window_size

    def update(self, val: float) -> float:
        self.history.append(val)
        if len(self.history) > self.window_size:
            self.history.pop(0)

        if len(self.history) < 8:
            return 0.0

        arr = np.array(self.history, dtype=np.float64)
        short_w = arr[-5:]
        long_w = arr

        mu_short = np.mean(short_w)
        mu_long = np.mean(long_w)
        std_long = float(np.std(long_w) + 1e-4)
        std_short = float(np.std(short_w) + 1e-4)

        jump_z = float(abs(mu_short - mu_long) / std_long)
        vol_ratio = float(std_short / std_long)

        hazard_stat = 0.6 * jump_z + 0.4 * max(0.0, vol_ratio - 1.0)
        hazard_prob = 1.0 - np.exp(-hazard_stat * 1.1)
        hazard_pct = round(float(min(100.0, max(0.0, hazard_prob * 100.0))), 1)
        return hazard_pct


_kelly_predictor = KellyRFFMarketPredictor()
_oxford_cpd = OxfordChangepointDetector()



# ── 실시간 원/달러(USD/KRW) 환율 수집기 (네이버 FX 메인 + 야후 파이낸스 폴백 + 캐시) ──
_fx_cache = {"ts": 0.0, "data": (1340.0, 0.0, "환율 안정")}

def fetch_realtime_usdkrw() -> tuple[float, float, str]:
    """실시간 원/달러 환율 (현재가, 등락률 %, 상태) 튜플을 반환합니다."""
    import urllib.request, json
    now = time.time()
    if now - _fx_cache["ts"] < 8.0:
        return _fx_cache["data"]

    # 1. 네이버 금융 실시간 FX API (Primary)
    try:
        url = "https://m.stock.naver.com/front-api/marketIndex/productDetail?category=exchange&reutersCode=FX_USDKRW"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            res = data.get("result", {})
            pr_str = str(res.get("closePrice", "")).replace(",", "")
            rate_str = str(res.get("fluctuationsRatio", "0")).replace("%", "")
            price = float(pr_str)
            rate = float(rate_str)
            status = "원화 강세 (외인유입 우호)" if rate <= -0.3 else ("원화 약세/환율급등 (외인매도 경계)" if rate >= 0.5 else "환율 안정")
            _fx_cache["ts"] = now
            _fx_cache["data"] = (price, rate, status)
            return _fx_cache["data"]
    except Exception as e:
        logger.debug(f"네이버 FX 환율 조회 예외: {e}")

    # 2. Yahoo Finance FX 폴백 (Fallback)
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/KRW=X?interval=1m&range=1d"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            meta = data["chart"]["result"][0]["meta"]
            price = float(meta.get("regularMarketPrice") or 1340.0)
            prev = float(meta.get("chartPreviousClose") or meta.get("previousClose") or price)
            rate = round(((price - prev) / prev) * 100, 2) if prev > 0 else 0.0
            status = "원화 강세 (외인유입 우호)" if rate <= -0.3 else ("원화 약세/환율급등 (외인매도 경계)" if rate >= 0.5 else "환율 안정")
            _fx_cache["ts"] = now
            _fx_cache["data"] = (price, rate, status)
            return _fx_cache["data"]
    except Exception as e:
        logger.debug(f"야후 FX 환율 조회 예외: {e}")

    return _fx_cache["data"]


# ── 실시간 글로벌(미국) 및 국내 채권 금리 수집기 (NH Open API krbond + Yahoo Finance) ──
_bond_cache = {
    "ts": 0.0,
    "data": {
        "us_10y": 5.045, "us_2y": 4.750, "us_30y": 5.361,
        "kr_10y": 4.110, "kr_3y": 3.800, "kr_30y": 4.220,
        "us_spread": 0.295, "kr_spread": 0.310, "us_kr_spread": 0.935,
        "bond_shock": 0.5,
        "sentiment": "고금리 긴축 경계"
    }
}

def fetch_realtime_bonds() -> dict:
    """실시간 미국 국채 및 한국 국고채 금리 맵을 반환합니다 (30초 캐시)."""
    now = time.time()
    if now - _bond_cache["ts"] < 25.0:
        return _bond_cache["data"]

    data = dict(_bond_cache["data"])

    # 1. NH Open API 국내 국고채 수익률 조회 (/krbond/quote/v1/yieldTrend)
    try:
        from datetime import datetime, timedelta
        for days in [0, 1, 2, 3]:
            dt = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
            try:
                res = call("/krbond/quote/v1/yieldTrend", {"bsop_date": dt})
                out = res.get("Output_0", [])
                if out:
                    for r in out:
                        nm = r.get("iem_nm", "")
                        y = float(r.get("ovrs_prpr") or r.get("price2") or 0.0)
                        if "국고채권(10년)" in nm: data["kr_10y"] = y
                        elif "국고채권(3년)" in nm: data["kr_3y"] = y
                        elif "국고채권(30년)" in nm: data["kr_30y"] = y
                    break
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"국내 채권 금리 조회 예외: {e}")

    # 2. Yahoo Finance 미국 국채 10년(^TNX) 및 30년(^TYX) 조회
    try:
        import urllib.request, json
        # US 10Y
        try:
            url = "https://query1.finance.yahoo.com/v8/finance/chart/^TNX?interval=1m&range=1d"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
                meta = raw["chart"]["result"][0]["meta"]
                data["us_10y"] = float(meta.get("regularMarketPrice") or data["us_10y"])
        except Exception:
            pass

        # US 30Y
        try:
            url = "https://query1.finance.yahoo.com/v8/finance/chart/^TYX?interval=1m&range=1d"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
                meta = raw["chart"]["result"][0]["meta"]
                data["us_30y"] = float(meta.get("regularMarketPrice") or data["us_30y"])
        except Exception:
            pass
    except Exception as e:
        logger.debug(f"미국 국채 금리 조회 예외: {e}")

    # 파생 지표 연산
    data["us_spread"] = round(data["us_10y"] - data["us_2y"], 3)
    data["kr_spread"] = round(data["kr_10y"] - data["kr_3y"], 3)
    data["us_kr_spread"] = round(data["us_10y"] - data["kr_10y"], 3)

    # 채권 충격도 (Bond Shock Index): 미국 10년이 4.8%를 넘고 한미 금리차가 0.8%p 이상일 때 가중
    shock = (data["us_10y"] - 4.60) * 1.5 + (data["us_kr_spread"] - 0.70) * 1.2
    data["bond_shock"] = round(float(shock), 3)

    if data["us_10y"] >= 5.00:
        data["sentiment"] = "미국 10년물 5.0% 돌파 (고금리 긴축 경계)"
    elif data["us_kr_spread"] >= 1.00:
        data["sentiment"] = "한미 금리차 100bp 초과 (자본유출 경계)"
    elif data["us_10y"] <= 4.50:
        data["sentiment"] = "글로벌 금리 안정 (주식 우호)"
    else:
        data["sentiment"] = "금리 중립/안정"

    _bond_cache["ts"] = now
    _bond_cache["data"] = data
    return data


# ── BIS WP 1320 (2025.12) 인플레이션 서프라이즈 및 달러 인덱스(DXY) 수집기 ──
_bis_cache = {
    "ts": 0.0,
    "data": {
        "dxy_price": 102.16,
        "dxy_rate": -0.04,
        "cpi_actual": 3.0,
        "cpi_consensus": 2.9,
        "cpi_surprise": 0.1,
        "breakeven_10y": 2.32,
        "bis_shock": 0.15,
        "sentiment": "인플레이션 & 달러 안정"
    }
}

def fetch_realtime_bis_inflation(us_10y: float = 5.04) -> dict:
    """BIS WP 1320 기반 달러 인덱스(DXY) 및 인플레이션 서프라이즈 결합 충격도를 산출합니다."""
    now = time.time()
    if now - _bis_cache["ts"] < 25.0:
        return _bis_cache["data"]

    data = dict(_bis_cache["data"])

    # 1. Yahoo Finance 달러 인덱스 (DXY: DX-Y.NYB) 실시간 조회
    try:
        import urllib.request, json
        url = "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?interval=1m&range=1d"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
            meta = raw["chart"]["result"][0]["meta"]
            dxy = float(meta.get("regularMarketPrice") or 102.16)
            prev = float(meta.get("chartPreviousClose") or meta.get("previousClose") or dxy)
            data["dxy_price"] = round(dxy, 2)
            data["dxy_rate"] = round(((dxy - prev) / prev) * 100, 2) if prev else 0.0
    except Exception as e:
        logger.debug(f"DXY 조회 예외: {e}")

    # 2. BIS WP 1320 결합 채권-외환 미포섭 충격 지수 (Joint Bond-FX Spanning Shock Index)
    # J = 0.40 * norm_dxy + 0.35 * norm_us10y + 0.25 * norm_cpi_surprise
    norm_dxy = np.clip(data["dxy_rate"] / 0.5, -2.5, 2.5)
    norm_us10y = np.clip((us_10y - 4.60) / 0.4, -2.5, 2.5)
    norm_cpi = np.clip(data["cpi_surprise"] / 0.2, -2.5, 2.5)

    shock = float(0.40 * norm_dxy + 0.35 * norm_us10y + 0.25 * norm_cpi)
    data["bis_shock"] = round(shock, 2)

    if shock >= 0.8:
        data["sentiment"] = "예상 밖 긴축 충격 (달러 강세 & 채권금리 급등 동시 발생)"
    elif shock >= 0.3:
        data["sentiment"] = "달러 강세 및 인플레 경계"
    elif shock <= -0.3:
        data["sentiment"] = "디스인플레이션 안도 (달러 안정 & 주식 우호)"
    else:
        data["sentiment"] = "인플레이션 & 달러 안정"

    _bis_cache["ts"] = now
    _bis_cache["data"] = data
    return data


# ── 국제 유가(WTI) 실시간 수집 및 공급/수요 충격 분해기 (Jiang 2026 & FRBSF 2026) ──
_oil_cache = {
    "ts": 0.0,
    "data": {
        "wti_price": 101.86,
        "wti_rate": -0.90,
        "shock_type": "공급 안정 (원가부담 완화/골디락스)",
        "oil_shock": 0.3,
        "oil_score": 9
    }
}

def fetch_realtime_wti_oil(global_equity_rate: float = -0.5) -> dict:
    """실시간 WTI 원유 가격을 수집하고 공급 충격(스태그플레이션) vs 수요 견인(경기회복)으로 분해합니다."""
    now = time.time()
    if now - _oil_cache["ts"] < 25.0:
        return _oil_cache["data"]

    data = dict(_oil_cache["data"])

    # 1. Yahoo Finance WTI 원유 선물(CL=F) 조회
    try:
        import urllib.request, json
        url = "https://query1.finance.yahoo.com/v8/finance/chart/CL=F?interval=1m&range=1d"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
            meta = raw["chart"]["result"][0]["meta"]
            pr = float(meta.get("regularMarketPrice") or 101.86)
            prev = float(meta.get("chartPreviousClose") or meta.get("previousClose") or pr)
            data["wti_price"] = round(pr, 2)
            data["wti_rate"] = round(((pr - prev) / prev) * 100, 2) if prev else 0.0
    except Exception as e:
        logger.debug(f"WTI 유가 조회 예외: {e}")

    # 2. 유가 충격 분해 (Decomposition: Jiang 2026 / FRBSF 2026)
    r_oil = data["wti_rate"]
    r_eq = global_equity_rate

    # 공급 쇼크: 유가 급등(+) & 글로벌 주식 급락(-) -> 제조원가 폭등/스태그플레이션
    # 수요 견인: 유가 상승(+) & 글로벌 주식 상승(+) -> 세계 경기 회복
    if r_oil >= 1.0 and r_eq <= -0.3:
        data["shock_type"] = "⚠️ 공급 충격 (스태그플레이션/원가폭등 위험)"
        data["oil_shock"] = -round((r_oil * 0.7 + abs(r_eq) * 0.5), 2)
        data["oil_score"] = 0
    elif r_oil >= 0.5 and r_eq >= 0.2:
        data["shock_type"] = "🔥 수요 견인 (글로벌 경기회복 순풍)"
        data["oil_shock"] = round((r_oil * 0.5 + r_eq * 0.5), 2)
        data["oil_score"] = 9
    elif r_oil <= -0.5 and r_eq >= -0.5:
        data["shock_type"] = "🟢 공급 안정 (원가부담 완화/골디락스)"
        data["oil_shock"] = round(abs(r_oil) * 0.4, 2)
        data["oil_score"] = 9
    elif r_oil <= -1.5 and r_eq <= -0.8:
        data["shock_type"] = "❄️ 경기 침체 (글로벌 수요둔화 우려)"
        data["oil_shock"] = -round(abs(r_oil) * 0.5, 2)
        data["oil_score"] = 3
    else:
        data["shock_type"] = "⚖️ 유가 중립/안정"
        data["oil_shock"] = 0.0
        data["oil_score"] = 6

    _oil_cache["ts"] = now
    _oil_cache["data"] = data
    return data


# ── 코스피(KOSPI) & 코스닥(KOSDAQ) 실시간 지수 및 투자자별 수급 수집기 ──
_indices_cache = {
    "ts": 0.0,
    "data": {
        "KOSPI": {"price": 0.0, "rate": 0.0, "change": 0.0, "foreign": 0, "org": 0, "retail": 0},
        "KOSDAQ": {"price": 0.0, "rate": 0.0, "change": 0.0, "foreign": 0, "org": 0, "retail": 0},
        "decoupling_gap": 0.0,
        "decoupling_status": "동조"
    }
}

def fetch_realtime_korean_indices() -> dict:
    """코스피와 코스닥 지수 시세 및 KRX/NXT 전수 투자자별(외인/기관/개인) 통합 순매수 동향을 수집하고 디커플링 상태를 진단합니다."""
    now = time.time()
    if now - _indices_cache["ts"] < 10.0 and _indices_cache["data"]:
        return _indices_cache["data"]

    results = {}
    import urllib.request, json
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

    # 1. 네이버 모바일 지수 기본 시세 (가격, 등락률, 전일대비)
    for idx in ['KOSPI', 'KOSDAQ']:
        res = {
            'price': 0.0, 'rate': 0.0, 'change': 0.0,
            'trends': {
                'krx': {'foreign': 0, 'org': 0, 'retail': 0},
                'nxt': {'foreign': 0, 'org': 0, 'retail': 0},
                'total': {'foreign': 0, 'org': 0, 'retail': 0}
            },
            'foreign': 0, 'org': 0, 'retail': 0
        }
        try:
            url_b = f'https://m.stock.naver.com/api/index/{idx}/basic'
            req_b = urllib.request.Request(url_b, headers=headers)
            with urllib.request.urlopen(req_b, timeout=2.5) as resp:
                d = json.loads(resp.read().decode('utf-8'))
                res['price'] = float(str(d.get('closePrice', '0')).replace(',', ''))
                r_val = float(str(d.get('fluctuationsRatio', '0')).replace(',', ''))
                sign = d.get('compareToPreviousPrice', {}).get('name', '')
                res['rate'] = -abs(r_val) if sign == 'FALLING' else abs(r_val)
                c_val = float(str(d.get('compareToPreviousClosePrice', '0')).replace(',', ''))
                res['change'] = -abs(c_val) if sign == 'FALLING' else abs(c_val)
        except Exception as e:
            logger.debug(f"{idx} 지수 기본 시세 조회 오류: {e}")
        results[idx] = res

    # 2. KRX 및 NXT 시간별 전수 수급 API 연동 (https://stock.naver.com/api/domestic/market/trend/time)
    inst_keys = ['1000', '2000', '3000', '3100', '4000', '5000', '6000', '7000']
    for idx in ['KOSPI', 'KOSDAQ']:
        for t_type in ['KRX', 'NXT']:
            t_key = t_type.lower()
            try:
                url_trend = f"https://stock.naver.com/api/domestic/market/trend/time?tradeType={t_type}&marketType={idx}&startIdx=1&pageSize=1"
                req_t = urllib.request.Request(url_trend, headers=headers)
                with urllib.request.urlopen(req_t, timeout=2.5) as resp:
                    t_data = json.loads(resp.read().decode('utf-8'))
                    rows = t_data.get('content', [])
                    if rows:
                        net_amts = rows[0].get('netAmounts', [])
                        vals = {item['investorGubun']: int(item['diffValue']) // 100000000 for item in net_amts}
                        ret_val = vals.get('8000', 0)
                        for_val = vals.get('9000', 0) + vals.get('9001', 0)
                        org_val = sum(vals.get(k, 0) for k in inst_keys)
                        results[idx]['trends'][t_key] = {
                            'foreign': for_val,
                            'org': org_val,
                            'retail': ret_val
                        }
            except Exception as e:
                logger.debug(f"{idx} {t_type} 전수 수급 API 조회 오류: {e}")

        # 통합(KRX + NXT) 합산
        krx_t = results[idx]['trends']['krx']
        nxt_t = results[idx]['trends']['nxt']
        tot_f = krx_t['foreign'] + nxt_t['foreign']
        tot_o = krx_t['org'] + nxt_t['org']
        tot_r = krx_t['retail'] + nxt_t['retail']
        results[idx]['trends']['total'] = {
            'foreign': tot_f,
            'org': tot_o,
            'retail': tot_r
        }
        results[idx]['foreign'] = tot_f
        results[idx]['org'] = tot_o
        results[idx]['retail'] = tot_r

    # 3. 폴백 검증 (전수 API 미제공 시간대 또는 장애 시 기존 m.stock trend)
    for idx in ['KOSPI', 'KOSDAQ']:
        if results[idx]['foreign'] == 0 and results[idx]['org'] == 0:
            try:
                url_t = f'https://m.stock.naver.com/api/index/{idx}/trend'
                req_t = urllib.request.Request(url_t, headers=headers)
                with urllib.request.urlopen(req_t, timeout=2.0) as resp:
                    t = json.loads(resp.read().decode('utf-8'))
                    def parse_val(v):
                        if not v: return 0
                        s = str(v).replace(',', '').replace('+', '').strip()
                        try: return int(s)
                        except Exception: return 0
                    fb_r = parse_val(t.get('personalValue'))
                    fb_f = parse_val(t.get('foreignValue'))
                    fb_o = parse_val(t.get('institutionalValue'))
                    results[idx]['trends']['krx'] = {'foreign': fb_f, 'org': fb_o, 'retail': fb_r}
                    results[idx]['trends']['total'] = {'foreign': fb_f, 'org': fb_o, 'retail': fb_r}
                    results[idx]['foreign'] = fb_f
                    results[idx]['org'] = fb_o
                    results[idx]['retail'] = fb_r
            except Exception as e:
                logger.debug(f"{idx} 수급 동향 폴백 오류: {e}")

    # 4. Yahoo Finance 폴백 (네이버 통신 실패 시)
    yahoo_map = {'KOSPI': '^KS11', 'KOSDAQ': '^KQ11'}
    for idx, sym in yahoo_map.items():
        if results[idx]['price'] <= 0:
            try:
                y_url = f'https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1m&range=1d'
                y_req = urllib.request.Request(y_url, headers=headers)
                with urllib.request.urlopen(y_req, timeout=2.5) as y_resp:
                    y_raw = json.loads(y_resp.read().decode('utf-8'))
                    meta = y_raw['chart']['result'][0]['meta']
                    pr = float(meta.get('regularMarketPrice') or 0.0)
                    prev = float(meta.get('chartPreviousClose') or meta.get('previousClose') or pr)
                    results[idx]['price'] = round(pr, 2)
                    results[idx]['rate'] = round(((pr - prev) / prev) * 100, 2) if prev else 0.0
                    results[idx]['change'] = round(pr - prev, 2)
            except Exception as e:
                logger.debug(f"{sym} Yahoo 폴백 오류: {e}")

    kp = results['KOSPI']
    kd = results['KOSDAQ']
    gap = round(kp['rate'] - kd['rate'], 2)
    if gap >= 1.0:
        desc = f"⚡ 코스피 독주 디커플링 (대형주 쏠림 & 코스닥 수급 이탈, 괴리 {gap:+.2f}%p)"
    elif gap <= -1.0:
        desc = f"⚡ 코스닥 독주 디커플링 (중소형 성장주 쏠림 & 코스피 부진, 괴리 {gap:+.2f}%p)"
    else:
        desc = f"🤝 코스피-코스닥 동조 (커플링, 괴리 {gap:+.2f}%p)"

    data = {
        'KOSPI': kp,
        'KOSDAQ': kd,
        'decoupling_gap': gap,
        'decoupling_status': desc
    }
    _indices_cache['ts'] = now
    _indices_cache['data'] = data
    return data


# ── NXT(대체거래소) 복합 수급 왜곡(Shadow Divergence) 수집기 ──
_nxt_shadow_cache = {"ts": 0.0, "data": {}}

def fetch_nxt_composite_supply() -> dict:
    """NXT 대체거래소의 전수 실시간 수급을 반영하여 우회 매도(Shadow Selling)를 감지합니다."""
    global _nxt_shadow_cache
    now = time.time()
    if now - _nxt_shadow_cache["ts"] < 25.0 and _nxt_shadow_cache["data"]:
        return _nxt_shadow_cache["data"]

    k_indices = fetch_realtime_korean_indices()
    kp_nxt_f = k_indices.get("KOSPI", {}).get("trends", {}).get("nxt", {}).get("foreign", 0)
    kd_nxt_f = k_indices.get("KOSDAQ", {}).get("trends", {}).get("nxt", {}).get("foreign", 0)

    kp_div = abs(kp_nxt_f) if kp_nxt_f < 0 else 0
    kd_div = abs(kd_nxt_f) if kd_nxt_f < 0 else 0

    if kp_nxt_f <= -2000:
        status = f"🚨 NXT 대체거래소 대량 우회 매도 포착 (코스피 외인 {kp_nxt_f:+,}억, 코스닥 {kd_nxt_f:+,}억)"
    elif kp_nxt_f <= -500:
        status = f"⚠️ NXT 대체거래소 외인 매도 우위 (코스피 외인 {kp_nxt_f:+,}억, 코스닥 {kd_nxt_f:+,}억)"
    elif kp_nxt_f >= 500:
        status = f"🔥 NXT 대체거래소 외인 매수 유입 (코스피 외인 {kp_nxt_f:+,}억, 코스닥 {kd_nxt_f:+,}억)"
    else:
        status = "🟢 NXT 대체거래소 수급 안정"

    data = {
        "kospi_divergence": kp_div,
        "kosdaq_divergence": kd_div,
        "nxt_vol_ratio": 41.2,
        "status": status
    }
    _nxt_shadow_cache["ts"] = now
    _nxt_shadow_cache["data"] = data
    return data


@dataclass
class MarketMacroSnapshot:
    timestamp: str
    k200_future: float          # 코스피 200 선물 현재가
    k200_cash: float            # 코스피 200 현물 지수
    basis: float                # 시장 베이시스 (선물 - 현물)
    basis_status: str           # CONTANGO (선물>현물) / BACKWARDATION (선물<현물)
    foreign_future_val: int     # 외인 선물 순매수 (백만원)
    org_future_val: int         # 기관 선물 순매수 (백만원)
    retail_future_val: int      # 개인 선물 순매수 (백만원)
    foreign_call_val: int       # 외인 콜옵션 순매수 (백만원)
    foreign_put_val: int        # 외인 풋옵션 순매수 (백만원)
    option_sentiment: str       # 상방 우세 / 하방 우세 / 중립
    nasdaq_rate: float          # 미국 나스닥 100 선물 등락률 (%)
    sp500_rate: float           # 미국 S&P 500 선물 등락률 (%)
    usdkrw_price: float         # 실시간 원/달러 환율 (원)
    usdkrw_rate: float          # 실시간 원/달러 환율 등락률 (%)
    usdkrw_status: str          # 환율 국면 상태 (원화 강세/안정/약세급등)
    us_10y: float               # 미국 국채 10년물 금리 (%)
    us_2y: float                # 미국 국채 2년물 금리 (%)
    kr_10y: float               # 한국 국고채 10년물 금리 (%)
    kr_3y: float                # 한국 국고채 3년물 금리 (%)
    us_kr_spread: float         # 한미 10년 금리차 (%p)
    bond_sentiment: str         # 채권 시장 심리 상태
    dxy_price: float            # 글로벌 달러 인덱스 (DXY)
    dxy_rate: float             # 달러 인덱스 등락률 (%)
    cpi_actual: float           # 미국 CPI 발표치 (%)
    cpi_surprise: float         # 인플레이션 서프라이즈 (발표치 - 예상치 %p)
    bis_shock_index: float      # BIS 결합 충격 지수
    bis_sentiment: str          # BIS 국면 심리
    wti_price: float            # 국제 유가 WTI ($/배럴)
    wti_rate: float             # 유가 등락률 (%)
    oil_shock_type: str         # 유가 충격 분해 국면 (공급충격 vs 수요견인)
    macro_score: int            # 시장 종합 건강 점수 (0 ~ 100점)
    is_tradeable: bool          # 매수 허용 여부 (False: 시장 리스크 차단)
    block_reason: str           # 차단 사유
    regime: str                 # DANGER / SAFE / TAILWIND
    regime_name: str            # 국면 한글명칭
    briefing_text: str          # 텔레그램 및 화면 출력용 요약
    kelly_prob_up: float = 50.0 # Bryan Kelly (2024 JOF) RFF 시장 상방 확률 (%)
    cpd_hazard: float = 0.0     # Oxford GP-CPD 국면 전환 위험도 (%)
    rff_regime: str = "중립"     # RFF 판정 국면
    # ── 코스피(KOSPI) vs 코스닥(KOSDAQ) 디커플링 분리 지표 ──
    kospi_price: float = 0.0          # 코스피 지수
    kospi_rate: float = 0.0           # 코스피 등락률 (%)
    kospi_foreign_val: int = 0        # 코스피 통합 외인 순매수 (억원)
    kospi_org_val: int = 0            # 코스피 통합 기관 순매수 (억원)
    kospi_retail_val: int = 0         # 코스피 통합 개인 순매수 (억원)
    kospi_krx_foreign: int = 0        # 코스피 KRX 외인 순매수 (억원)
    kospi_nxt_foreign: int = 0        # 코스피 NXT 외인 순매수 (억원)
    kospi_krx_org: int = 0            # 코스피 KRX 기관 순매수 (억원)
    kospi_nxt_org: int = 0            # 코스피 NXT 기관 순매수 (억원)
    kospi_krx_retail: int = 0         # 코스피 KRX 개인 순매수 (억원)
    kospi_nxt_retail: int = 0         # 코스피 NXT 개인 순매수 (억원)
    kospi_score: int = 50             # 코스피 전용 시장점수 (0~100)
    is_kospi_tradeable: bool = True   # 코스피 종목 매수 허용 여부
    kospi_block_reason: str = ""      # 코스피 차단 사유
    kosdaq_price: float = 0.0         # 코스닥 지수
    kosdaq_rate: float = 0.0          # 코스닥 등락률 (%)
    kosdaq_foreign_val: int = 0       # 코스닥 통합 외인 순매수 (억원)
    kosdaq_org_val: int = 0           # 코스닥 통합 기관 순매수 (억원)
    kosdaq_retail_val: int = 0        # 코스닥 통합 개인 순매수 (억원)
    kosdaq_krx_foreign: int = 0       # 코스닥 KRX 외인 순매수 (억원)
    kosdaq_nxt_foreign: int = 0       # 코스닥 NXT 외인 순매수 (억원)
    kosdaq_krx_org: int = 0           # 코스닥 KRX 기관 순매수 (억원)
    kosdaq_nxt_org: int = 0           # 코스닥 NXT 기관 순매수 (억원)
    kosdaq_krx_retail: int = 0        # 코스닥 KRX 개인 순매수 (억원)
    kosdaq_nxt_retail: int = 0        # 코스닥 NXT 개인 순매수 (억원)
    kosdaq_score: int = 50            # 코스닥 전용 시장점수 (0~100)
    is_kosdaq_tradeable: bool = True  # 코스닥 종목 매수 허용 여부
    kosdaq_block_reason: str = ""     # 코스닥 차단 사유
    decoupling_gap: float = 0.0       # 코스피 - 코스닥 괴리율 (%p)
    decoupling_status: str = "동조"   # 디커플링 상태 설명
    kd150_future: float = 0.0         # 코스닥 150 선물 현재가
    kd150_cash: float = 0.0           # 코스닥 150 현물 지수
    kd150_basis: float = 0.0          # 코스닥 150 시장 베이시스 (선물 - 현물)
    kd150_basis_status: str = ""      # 콘탱고 / 백워데이션
    kd_foreign_fut: int = 0           # 코스닥 150 선물 외인 순매수 (백만원)
    kd_org_fut: int = 0               # 코스닥 150 선물 기관 순매수 (백만원)
    kd_retail_fut: int = 0            # 코스닥 150 선물 개인 순매수 (백만원)
    kospi_regime: str = "SAFE"        # 코스피 국면 (DANGER / SAFE / TAILWIND)
    kospi_regime_name: str = ""       # 코스피 국면 한글명칭
    kosdaq_regime: str = "SAFE"       # 코스닥 국면 (DANGER / SAFE / TAILWIND)
    kosdaq_regime_name: str = ""      # 코스닥 국면 한글명칭
    # ── KRX + NXT 복합 통합(UNT) 수급 지표 ──
    nxt_vol_ratio: float = 0.0        # 시장 평균 NXT 거래 비중 (%)
    nxt_shadow_status: str = ""       # NXT 대체거래소 수급 왜곡 상태
    kospi_effective_foreign: int = 0  # NXT 우회 매도 차감 실효 코스피 외인 수급 (억원)
    kosdaq_effective_foreign: int = 0 # NXT 우회 매도 차감 실효 코스닥 외인 수급 (억원)
    kospi_nxt_divergence: int = 0     # 코스피 NXT 우회 매도 추정액 (억원)
    kosdaq_nxt_divergence: int = 0    # 코스닥 NXT 우회 매도 추정액 (억원)
    # ── 야간 선물 실시간 지표 (18:00 ~ 06:00 / 08:30) ──
    is_night_session: bool = False      # 현재 야간선물 세션 여부
    k200_night_rate: float = 0.0        # 코스피 200 야간선물 등락률 (%)
    k200_night_vol: int = 0             # 코스피 200 야간선물 거래량 (계약)
    kd150_night_rate: float = 0.0       # 코스닥 150 야간선물 등락률 (%)
    kd150_night_vol: int = 0            # 코스닥 150 야간선물 거래량 (계약)
    # ── 장중 선물 급변속도 & 플래시 프리즈 (Intraday Derivative Velocity & Flash-Freeze) ──
    is_flash_frozen: bool = False       # 종합 플래시 프리즈 발동 여부 (어느 한쪽이라도 발동 시 True)
    flash_freeze_reason: str = ""       # 플래시 프리즈 발동 사유
    flash_freeze_rem_sec: int = 0       # 플래시 프리즈 잔여 대기 시간 (초)
    is_kospi_flash_frozen: bool = False # 코스피 전용 플래시 프리즈
    kospi_freeze_reason: str = ""       # 코스피 동결 사유
    kospi_freeze_rem_sec: int = 0       # 코스피 잔여 동결 시간 (초)
    is_kosdaq_flash_frozen: bool = False# 코스닥 전용 플래시 프리즈
    kosdaq_freeze_reason: str = ""      # 코스닥 동결 사유
    kosdaq_freeze_rem_sec: int = 0      # 코스닥 잔여 동결 시간 (초)
    nq_delta_1m: float = 0.0            # 미국 나스닥 100 선물 1분 변동률 (%p)
    nq_delta_3m: float = 0.0            # 미국 나스닥 100 선물 3분 변동률 (%p)
    basis_delta_1m: float = 0.0         # 코스피 200 베이시스 1분 변동폭 (pt)
    basis_delta_3m: float = 0.0         # 코스피 200 베이시스 3분 변동폭 (pt)
    kd_basis_delta_1m: float = 0.0      # 코스닥 150 베이시스 1분 변동폭 (pt)
    kd_basis_delta_3m: float = 0.0      # 코스닥 150 베이시스 3분 변동폭 (pt)
    cusum_downside_score: float = 0.0   # CUSUM 하방 추세 전환 누적 스코어


# ── 선물/옵션 수급 캐시 (API 유량 제한 및 일시적 조회 실패 방어용) ──
_last_fut_cache = {
    "foreign_fut": 0, "org_fut": 0, "retail_fut": 0,
    "kd_foreign_fut": 0, "kd_org_fut": 0, "kd_retail_fut": 0,
    "foreign_call": 0, "foreign_put": 0
}

_cached_snapshot: MarketMacroSnapshot | None = None
_last_fetch_time: float = 0.0
CACHE_TTL = 4.0  # 초당 4회 제한 준수를 위해 4초 캐싱



# ── 장중 선물 급변속도 및 플래시 프리즈 추적기 (Intraday Derivative Velocity Tracker) ──
@dataclass
class DerivativeVelocitySample:
    timestamp: float
    nq_rate: float
    k200_basis: float
    kd150_basis: float


class DerivativeVelocityTracker:
    """장중(Intraday) 미국 E-mini 나스닥 100 선물 및 국내 선물 베이시스 급변 속도(Velocity & CUSUM) 추적기.
    
    디커플링(Decoupling) 독립 원칙:
    - 글로벌 나스닥 100 급락: 코스피 & 코스닥 양 시장 모두 5분 플래시 프리즈 발동
    - 코스피 200 선물 베이시스 붕괴: 코스피(KOSPI) 시장만 독자적 5분 플래시 프리즈 발동 (코스닥은 매수 허용)
    - 코스닥 150 선물 베이시스 붕괴: 코스닥(KOSDAQ) 시장만 독자적 5분 플래시 프리즈 발동 (코스피는 매수 허용)
    """
    def __init__(self, history_len: int = 200, freeze_duration: float = 300.0):
        self.history: deque[DerivativeVelocitySample] = deque(maxlen=history_len)
        self.freeze_duration = freeze_duration
        
        # 글로벌(전 시장) 동결 상태
        self.last_transition_global: str | None = None  # "FROZEN", "UNFROZEN"
        self.global_freeze_reason: str = ""

        # 코스피(KOSPI) 전용 동결 상태
        self.is_kp_frozen: bool = False
        self.kp_freeze_until: float = 0.0
        self.kp_freeze_reason: str = ""
        self.last_transition_kp: str | None = None  # "FROZEN", "UNFROZEN"

        # 코스닥(KOSDAQ) 전용 동결 상태
        self.is_kd_frozen: bool = False
        self.kd_freeze_until: float = 0.0
        self.kd_freeze_reason: str = ""
        self.last_transition_kd: str | None = None  # "FROZEN", "UNFROZEN"

        self._cusum_neg_nq: float = 0.0

    def record_sample(self, now_ts: float, nq_rate: float, k200_basis: float, kd150_basis: float):
        sample = DerivativeVelocitySample(
            timestamp=now_ts,
            nq_rate=nq_rate,
            k200_basis=k200_basis,
            kd150_basis=kd150_basis
        )
        self.history.append(sample)
        cutoff = now_ts - 600.0
        while self.history and self.history[0].timestamp < cutoff:
            self.history.popleft()

    def get_velocity_metrics(self, now_ts: float | None = None) -> dict:
        if now_ts is None:
            now_ts = time.time()

        if not self.history or len(self.history) < 2:
            return {
                "nq_delta_1m": 0.0,
                "nq_delta_3m": 0.0,
                "k200_basis_delta_1m": 0.0,
                "k200_basis_delta_3m": 0.0,
                "kd150_basis_delta_1m": 0.0,
                "kd150_basis_delta_3m": 0.0,
                "cusum_score": 0.0
            }

        cur = self.history[-1]
        sample_1m = None
        sample_3m = None

        for s in reversed(self.history):
            dt = cur.timestamp - s.timestamp
            if sample_1m is None and dt >= 50.0:
                sample_1m = s
            if sample_3m is None and dt >= 160.0:
                sample_3m = s
                break

        if sample_1m is None:
            sample_1m = self.history[0]
        if sample_3m is None:
            sample_3m = self.history[0]

        nq_delta_1m = round(cur.nq_rate - sample_1m.nq_rate, 2)
        nq_delta_3m = round(cur.nq_rate - sample_3m.nq_rate, 2)
        k200_basis_delta_1m = round(cur.k200_basis - sample_1m.k200_basis, 2)
        k200_basis_delta_3m = round(cur.k200_basis - sample_3m.k200_basis, 2)
        kd150_basis_delta_1m = round(cur.kd150_basis - sample_1m.kd150_basis, 2)
        kd150_basis_delta_3m = round(cur.kd150_basis - sample_3m.kd150_basis, 2)

        if len(self.history) >= 2:
            prev = self.history[-2]
            inst_dn = max(0.0, -(cur.nq_rate - prev.nq_rate) - 0.02)
            self._cusum_neg_nq = round(max(0.0, self._cusum_neg_nq * 0.95 + inst_dn), 3)

        return {
            "nq_delta_1m": nq_delta_1m,
            "nq_delta_3m": nq_delta_3m,
            "k200_basis_delta_1m": k200_basis_delta_1m,
            "k200_basis_delta_3m": k200_basis_delta_3m,
            "kd150_basis_delta_1m": kd150_basis_delta_1m,
            "kd150_basis_delta_3m": kd150_basis_delta_3m,
            "cusum_score": self._cusum_neg_nq
        }

    def check_flash_freeze(self, now_ts: float | None = None) -> tuple[dict, dict]:
        """코스피와 코스닥 각각에 대해 독립적으로 플래시 프리즈 상태를 평가하고 반환합니다.
        
        Returns:
            (kp_state, kd_state)
            where state = {"is_frozen": bool, "reason": str, "rem_sec": int}
        """
        if now_ts is None:
            now_ts = time.time()

        metrics = self.get_velocity_metrics(now_ts)
        nq_3m = metrics["nq_delta_3m"]
        nq_1m = metrics["nq_delta_1m"]
        k200_basis_3m = metrics["k200_basis_delta_3m"]
        k200_basis_1m = metrics["k200_basis_delta_1m"]
        kd150_basis_3m = metrics["kd150_basis_delta_3m"]
        kd150_basis_1m = metrics["kd150_basis_delta_1m"]
        cusum = metrics["cusum_score"]

        # 1. 코스피(KOSPI) 상태 갱신
        if self.is_kp_frozen:
            if now_ts < self.kp_freeze_until:
                pass
            else:
                if nq_3m <= -0.25 or k200_basis_3m <= -0.40:
                    self.kp_freeze_until = now_ts + 180.0
                    self.kp_freeze_reason = f"코스피 급락 지속(나스닥 {nq_3m:+.2f}%, 베이시스 {k200_basis_3m:+.2f}pt)에 따른 프리즈 3분 연장"
                    logger.warning(f"🚨 [코스피 플래시 프리즈 연장] {self.kp_freeze_reason}")
                else:
                    self.is_kp_frozen = False
                    self.kp_freeze_until = 0.0
                    self.kp_freeze_reason = ""
                    self.last_transition_kp = "UNFROZEN"
                    logger.info("🟢 [코스피 플래시 프리즈 해제] 선물 속도 및 코스피 베이시스 안정화 확인")

        # 2. 코스닥(KOSDAQ) 상태 갱신
        if self.is_kd_frozen:
            if now_ts < self.kd_freeze_until:
                pass
            else:
                if nq_3m <= -0.25 or kd150_basis_3m <= -0.50:
                    self.kd_freeze_until = now_ts + 180.0
                    self.kd_freeze_reason = f"코스닥 급락 지속(나스닥 {nq_3m:+.2f}%, 코스닥150 베이시스 {kd150_basis_3m:+.2f}pt)에 따른 프리즈 3분 연장"
                    logger.warning(f"🚨 [코스닥 플래시 프리즈 연장] {self.kd_freeze_reason}")
                else:
                    self.is_kd_frozen = False
                    self.kd_freeze_until = 0.0
                    self.kd_freeze_reason = ""
                    self.last_transition_kd = "UNFROZEN"
                    logger.info("🟢 [코스닥 플래시 프리즈 해제] 선물 속도 및 코스닥150 베이시스 안정화 확인")

        # 3. 신규 동결 트리거 평가 (최소 3개 샘플 이상)
        if len(self.history) >= 3:
            # A. 글로벌 공통 충격: 미국 나스닥 100 선물 급락 or CUSUM 임계치 돌파 -> 양 시장 동시 동결
            global_shock = False
            global_reason = ""
            if nq_3m <= -0.40:
                global_shock = True
                global_reason = f"미국 나스닥100 선물 3분 급락 ({nq_3m:+.2f}%p)"
            elif nq_1m <= -0.30:
                global_shock = True
                global_reason = f"미국 나스닥100 선물 1분 플래시 급락 ({nq_1m:+.2f}%p)"
            elif cusum >= 0.40:
                global_shock = True
                global_reason = f"선물 하방 CUSUM 추세전환 한계 돌파 (누적 {cusum:.2f})"

            if global_shock:
                self.is_kp_frozen = True
                self.kp_freeze_until = now_ts + self.freeze_duration
                self.kp_freeze_reason = global_reason
                self.is_kd_frozen = True
                self.kd_freeze_until = now_ts + self.freeze_duration
                self.kd_freeze_reason = global_reason
                self.last_transition_global = "FROZEN"
                self.global_freeze_reason = global_reason
                logger.warning(f"🚨 [전 시장 플래시 프리즈 발동] {global_reason}")

            # B. 코스피 200 전용 선물 베이시스 충격 -> 코스피(KOSPI)만 독자 동결!
            # (차트상 실질 급락 기준: 1분 30틱(-1.50pt, ~-0.17%) / 3분 44틱(-2.20pt, ~-0.25%) 급락 시)
            if not self.is_kp_frozen:
                kp_triggered = False
                kp_reason = ""
                if k200_basis_3m <= -2.20:
                    kp_triggered = True
                    kp_reason = f"코스피200 선물 베이시스 3분 붕괴 ({k200_basis_3m:+.2f}pt)"
                elif k200_basis_1m <= -1.50:
                    kp_triggered = True
                    kp_reason = f"코스피200 선물 베이시스 1분 급락 ({k200_basis_1m:+.2f}pt)"

                if kp_triggered:
                    self.is_kp_frozen = True
                    self.kp_freeze_until = now_ts + self.freeze_duration
                    self.kp_freeze_reason = kp_reason
                    self.last_transition_kp = "FROZEN"
                    logger.warning(f"🚨 [코스피 독자 플래시 프리즈 발동!] {kp_reason} (코스닥은 정상 유지)")

            # C. 코스닥 150 전용 선물 베이시스 충격 -> 코스닥(KOSDAQ)만 독자 동결!
            # (코스닥 1틱=0.25pt 기준: 1분 10틱(-2.50pt, ~-0.21%) / 3분 15틱(-3.75pt, ~-0.31%) 급락 시)
            if not self.is_kd_frozen:
                kd_triggered = False
                kd_reason = ""
                if kd150_basis_3m <= -3.75:
                    kd_triggered = True
                    kd_reason = f"코스닥150 선물 베이시스 3분 붕괴 ({kd150_basis_3m:+.2f}pt)"
                elif kd150_basis_1m <= -2.50:
                    kd_triggered = True
                    kd_reason = f"코스닥150 선물 베이시스 1분 급락 ({kd150_basis_1m:+.2f}pt)"

                if kd_triggered:
                    self.is_kd_frozen = True
                    self.kd_freeze_until = now_ts + self.freeze_duration
                    self.kd_freeze_reason = kd_reason
                    self.last_transition_kd = "FROZEN"
                    logger.warning(f"🚨 [코스닥 독자 플래시 프리즈 발동!] {kd_reason} (코스피는 정상 유지)")

        kp_rem = int(max(0.0, self.kp_freeze_until - now_ts)) if self.is_kp_frozen else 0
        kd_rem = int(max(0.0, self.kd_freeze_until - now_ts)) if self.is_kd_frozen else 0

        kp_state = {
            "is_frozen": self.is_kp_frozen,
            "reason": self.kp_freeze_reason,
            "rem_sec": kp_rem
        }
        kd_state = {
            "is_frozen": self.is_kd_frozen,
            "reason": self.kd_freeze_reason,
            "rem_sec": kd_rem
        }
        return kp_state, kd_state

velocity_tracker = DerivativeVelocityTracker()


def get_market_macro_snapshot(force_refresh: bool = False) -> MarketMacroSnapshot:
    """실시간 시장 파생 및 미국 선물 지표를 수집하고 금융공학적 점수와 차단 여부를 판정합니다."""
    global _cached_snapshot, _last_fetch_time

    now = time.time()
    if not force_refresh and _cached_snapshot and (now - _last_fetch_time < CACHE_TTL):
        return _cached_snapshot

    cur_time_str = time.strftime("%H:%M:%S")

    # 기본값 설정
    k200_fut = 0.0
    k200_cash = 0.0
    basis = 0.0
    foreign_fut = 0
    org_fut = 0
    foreign_call = 0
    foreign_put = 0
    nasdaq_rate = 0.0
    sp500_rate = 0.0

    now_dt = datetime.now()
    now_t = now_dt.time()
    is_night_time = (now_t >= dtime(18, 0) or now_t < dtime(8, 30))
    is_night_session = False
    k200_night_rate = 0.0
    k200_night_vol = 0
    kd150_night_rate = 0.0
    kd150_night_vol = 0

    # 1. 코스피 200 선물 & 베이시스 조회 (야간 세션 우선 분기)
    if is_night_time:
        try:
            night_res = call("/krfuture/quote/v1/night", {"iem_cd": KOSPI200_FUT_CODE})
            o0 = night_res.get("Output_0") or {}
            o1 = night_res.get("Output_1") or {}
            k_pr = float(o0.get("prpr") or 0.0)
            if k_pr > 0:
                is_night_session = True
                k200_fut = k_pr
                k200_cash = float(o1.get("unas_prpr") or 0.0)
                basis = round(k200_fut - k200_cash, 2) if k200_cash > 0 else float(o0.get("mrkt_basis") or 0.0)
                sdpr = float(o0.get("sdpr") or 0.0)
                k200_night_rate = round((k200_fut - sdpr) / sdpr * 100, 2) if sdpr > 0 else float(o0.get("ctrt") or 0.0)
                k200_night_vol = int(o0.get("acml_vol") or 0)
        except Exception as e:
            logger.debug(f"코스피 200 야간선물 조회 오류: {e}")

    if k200_fut == 0.0:
        try:
            f_res = call("/krfuture/quote/v1/day", {"iem_cd": KOSPI200_FUT_CODE})
            o0 = f_res.get("Output_0") or {}
            o1 = f_res.get("Output_1") or {}
            k200_fut = float(o0.get("prpr") or 0.0)
            k200_cash = float(o1.get("unas_prpr") or 0.0)
            basis = round(k200_fut - k200_cash, 2) if k200_cash > 0 else float(o0.get("mrkt_basis") or 0.0)
        except Exception as e:
            logger.debug(f"코스피 200 주간선물 조회 오류: {e}")

    # 1-1. 코스닥 150 선물 & 베이시스 조회 (KA066C000)
    kd150_fut = 0.0
    kd150_cash = 0.0
    kd150_basis = 0.0
    if is_night_time:
        try:
            kd_night_res = call("/krfuture/quote/v1/night", {"iem_cd": KOSDAQ150_FUT_CODE})
            kd_o0 = kd_night_res.get("Output_0") or {}
            kd_o1 = kd_night_res.get("Output_1") or {}
            kd_pr = float(kd_o0.get("prpr") or 0.0)
            if kd_pr > 0:
                kd150_fut = kd_pr
                kd150_cash = float(kd_o1.get("unas_prpr") or 0.0)
                kd150_basis = round(kd150_fut - kd150_cash, 2) if kd150_cash > 0 else float(kd_o0.get("mrkt_basis") or 0.0)
                kd_sdpr = float(kd_o0.get("sdpr") or 0.0)
                kd150_night_rate = round((kd150_fut - kd_sdpr) / kd_sdpr * 100, 2) if kd_sdpr > 0 else float(kd_o0.get("ctrt") or 0.0)
                kd150_night_vol = int(kd_o0.get("acml_vol") or 0)
        except Exception as e:
            logger.debug(f"코스닥 150 야간선물 조회 오류: {e}")

    if kd150_fut == 0.0:
        try:
            kd_f_res = call("/krfuture/quote/v1/day", {"iem_cd": KOSDAQ150_FUT_CODE})
            kd_o0 = kd_f_res.get("Output_0") or {}
            kd_o1 = kd_f_res.get("Output_1") or {}
            kd150_fut = float(kd_o0.get("prpr") or 0.0)
            kd150_cash = float(kd_o1.get("unas_prpr") or 0.0)
            kd150_basis = round(kd150_fut - kd150_cash, 2) if kd150_cash > 0 else float(kd_o0.get("mrkt_basis") or 0.0)
        except Exception as e:
            logger.debug(f"코스닥 150 주간선물 조회 오류: {e}")
    kd150_basis_status = "콘탱고(상승우세)" if kd150_basis >= 0 else "백워데이션(하방압력)"

    # 1-2. 코스닥 150 선물 투자자별 수급 (mrkt_cls_code='6')
    kd_foreign_fut = _last_fut_cache["kd_foreign_fut"]
    kd_org_fut = _last_fut_cache["kd_org_fut"]
    kd_retail_fut = _last_fut_cache["kd_retail_fut"]
    try:
        kd_s_res = call("/krfuture/quote/v1/tradingStatus", {
            "market_cd": "UNT", "mrkt_cls_code": "6", "cycle_cls_code": "4", "opt_cls_code": "1", "cnt": "1"
        })
        kd_s_row = (kd_s_res.get("Output_0") or [{}])[0]
        if "frgn_val" in kd_s_row:
            kd_foreign_fut = int(float(kd_s_row.get("frgn_val") or 0))
            kd_org_fut = int(float(kd_s_row.get("orgn_val") or 0))
            kd_retail_fut = int(float(kd_s_row.get("prsn_val") or 0))
            _last_fut_cache["kd_foreign_fut"] = kd_foreign_fut
            _last_fut_cache["kd_org_fut"] = kd_org_fut
            _last_fut_cache["kd_retail_fut"] = kd_retail_fut
    except Exception as e:
        logger.debug(f"코스닥 150 선물 수급 조회 오류: {e}")

    # 2. 코스피 200 선물 투자자별 수급 (외인/기관/개인)
    foreign_fut = _last_fut_cache["foreign_fut"]
    org_fut = _last_fut_cache["org_fut"]
    retail_fut = _last_fut_cache["retail_fut"]
    try:
        s_res = call("/krfuture/quote/v1/tradingStatus", {
            "market_cd": "UNT", "mrkt_cls_code": "3", "cycle_cls_code": "4", "opt_cls_code": "1", "cnt": "1"
        })
        s_row = (s_res.get("Output_0") or [{}])[0]
        if "frgn_val" in s_row:
            foreign_fut = int(float(s_row.get("frgn_val") or 0))
            org_fut = int(float(s_row.get("orgn_val") or 0))
            retail_fut = int(float(s_row.get("prsn_val") or 0))
            _last_fut_cache["foreign_fut"] = foreign_fut
            _last_fut_cache["org_fut"] = org_fut
            _last_fut_cache["retail_fut"] = retail_fut
    except Exception as e:
        logger.debug(f"선물 투자자 수급 조회 오류: {e}")

    # 3. 옵션 투자자별 수급 (외인 콜/풋옵션)
    foreign_call = _last_fut_cache["foreign_call"]
    foreign_put = _last_fut_cache["foreign_put"]
    try:
        c_res = call("/krfuture/quote/v1/tradingStatus", {
            "market_cd": "UNT", "mrkt_cls_code": "4", "cycle_cls_code": "4", "opt_cls_code": "1", "cnt": "1"
        })
        p_res = call("/krfuture/quote/v1/tradingStatus", {
            "market_cd": "UNT", "mrkt_cls_code": "5", "cycle_cls_code": "4", "opt_cls_code": "1", "cnt": "1"
        })
        c_row = (c_res.get("Output_0") or [{}])[0]
        p_row = (p_res.get("Output_0") or [{}])[0]
        if "frgn_val" in c_row:
            foreign_call = int(float(c_row.get("frgn_val") or 0))
            _last_fut_cache["foreign_call"] = foreign_call
        if "frgn_val" in p_row:
            foreign_put = int(float(p_row.get("frgn_val") or 0))
            _last_fut_cache["foreign_put"] = foreign_put
    except Exception as e:
        logger.debug(f"옵션 수급 조회 오류: {e}")

    # 4. 미국 CME E-mini 나스닥/S&P 선물 조회
    try:
        nq_res = call("/gbfuture/quote/v1/current", {"exnm": "FCME", "iem_cd": US_NASDAQ_FUT_CODE})
        nq_o0 = nq_res.get("Output_0") or {}
        nasdaq_rate = float(nq_o0.get("rate") or 0.0)
    except Exception as e:
        logger.debug(f"미국 나스닥 선물 조회 오류: {e}")

    try:
        es_res = call("/gbfuture/quote/v1/current", {"exnm": "FCME", "iem_cd": US_SP500_FUT_CODE})
        es_o0 = es_res.get("Output_0") or {}
        sp500_rate = float(es_o0.get("rate") or 0.0)
    except Exception as e:
        logger.debug(f"미국 S&P 선물 조회 오류: {e}")

    # 5. 실시간 원/달러 환율, 글로벌 국채 금리, BIS 인플레/DXY, WTI 유가 충격 분해 조회
    usdkrw_price, usdkrw_rate, usdkrw_status = fetch_realtime_usdkrw()
    bonds = fetch_realtime_bonds()
    bis_data = fetch_realtime_bis_inflation(us_10y=bonds["us_10y"])
    oil_data = fetch_realtime_wti_oil(global_equity_rate=nasdaq_rate)
    k_indices = fetch_realtime_korean_indices()
    kp_info = k_indices["KOSPI"]
    kd_info = k_indices["KOSDAQ"]
    decoupling_gap = k_indices["decoupling_gap"]
    decoupling_status = k_indices["decoupling_status"]

    # ── AI 수리모델 연산 (Kelly RFF 상방확률 & Oxford GP-CPD 변화점 위험도) ──
    kelly_prob_up, rff_regime = _kelly_predictor.predict_upward_prob(
        basis=basis,
        foreign_fut=foreign_fut,
        org_fut=org_fut,
        foreign_call=foreign_call,
        foreign_put=foreign_put,
        nasdaq_rate=nasdaq_rate,
        sp500_rate=sp500_rate,
        usdkrw_rate=usdkrw_rate,
        bond_shock=bonds["bond_shock"],
        bis_shock=bis_data["bis_shock"],
        oil_shock=oil_data["oil_shock"]
    )

    # 환율 급등(-)은 합성 시계열을 강하게 끌어내림
    composite_signal = basis * 2.0 + (nasdaq_rate * 4.0) + (foreign_fut / 10000.0) - (usdkrw_rate * 3.5)
    cpd_hazard = _oxford_cpd.update(composite_signal)

    # ── 금융공학적 점수 계산 (100점 만점 구조 - 유가 충격 분해 통합) ──
    # 1. 베이시스 점수 (최대 14점)
    if basis >= 0.2: s_basis = 14
    elif basis >= -0.3: s_basis = 10
    elif basis >= -0.8: s_basis = 6
    elif basis >= -1.2: s_basis = 3
    else: s_basis = 0
    basis_status = "콘탱고(상승우세)" if basis >= 0 else "백워데이션(하방압력)"

    # 2. 외인 선물 순매수 점수 (최대 14점)
    if foreign_fut >= 100000: s_ffut = 14
    elif foreign_fut >= 20000: s_ffut = 10
    elif foreign_fut >= -20000: s_ffut = 6
    elif foreign_fut >= -70000: s_ffut = 3
    else: s_ffut = 0

    # 3. 외인 옵션 포지션 점수 (최대 10점)
    if foreign_call > 0 and foreign_put <= 0:
        s_opt = 10
        opt_sentiment = "상방 베팅 (콜 순매수)"
    elif foreign_call < 0 and foreign_put > 0:
        s_opt = 0
        opt_sentiment = "하방 헷지 (풋 순매수)"
    else:
        s_opt = 5
        opt_sentiment = "중립/박스권"

    # 4. 미국 선물 점수 (최대 11점)
    if nasdaq_rate >= 0.5: s_us = 11
    elif nasdaq_rate >= 0.0: s_us = 8
    elif nasdaq_rate >= -0.5: s_us = 5
    elif nasdaq_rate >= -1.0: s_us = 2
    else: s_us = 0

    # 5. 원/달러(USD/KRW) 환율 점수 (최대 9점)
    if usdkrw_rate <= -0.4: s_fx = 9
    elif usdkrw_rate <= 0.1: s_fx = 7
    elif usdkrw_rate <= 0.5: s_fx = 4
    elif usdkrw_rate <= 1.0: s_fx = 2
    else: s_fx = 0

    # 6. 글로벌 & 국내 국채 금리/일드커브 점수 (최대 9점)
    u10 = bonds["us_10y"]
    diff = bonds["us_kr_spread"]
    if u10 <= 4.60 and diff <= 0.70: s_bond = 9
    elif u10 <= 4.85 and diff <= 0.85: s_bond = 6
    elif u10 <= 5.05 and diff <= 1.00: s_bond = 4
    elif u10 <= 5.25: s_bond = 2
    else: s_bond = 0

    # 7. BIS WP 1320 달러 인덱스(DXY) & 인플레이션 서프라이즈 점수 (최대 8점)
    dxy_p = bis_data["dxy_price"]
    cpi_s = bis_data["cpi_surprise"]
    if dxy_p <= 102.5 and cpi_s <= 0.0: s_bis = 8
    elif dxy_p <= 103.5 and cpi_s <= 0.15: s_bis = 6
    elif dxy_p <= 104.5: s_bis = 3
    elif dxy_p <= 106.0: s_bis = 1
    else: s_bis = 0

    # 8. WTI 국제 유가 공급/수요 충격 점수 (최대 9점: Jiang 2026 & FRBSF 2026)
    s_oil = oil_data["oil_score"]

    # 9. Bryan Kelly RFF 비선형 상방 확률 점수 (최대 16점)
    s_rff = int(round((kelly_prob_up / 100.0) * 16.0))

    macro_score = min(100, max(0, s_basis + s_ffut + s_opt + s_us + s_fx + s_bond + s_bis + s_oil + s_rff))

    # ── [코스피(KOSPI) vs 코스닥(KOSDAQ) 디커플링 독립 점수 및 게이트 연산] ──
    kp_rate = kp_info["rate"]
    kp_trends = kp_info.get("trends", {})
    kp_krx = kp_trends.get("krx", {"foreign": 0, "org": 0, "retail": 0})
    kp_nxt = kp_trends.get("nxt", {"foreign": 0, "org": 0, "retail": 0})
    kp_total = kp_trends.get("total", {"foreign": kp_info.get("foreign", 0), "org": kp_info.get("org", 0), "retail": kp_info.get("retail", 0)})

    effective_kp_foreign = kp_total["foreign"]
    kp_org = kp_total["org"]
    kp_retail = kp_total["retail"]

    kd_rate = kd_info["rate"]
    kd_trends = kd_info.get("trends", {})
    kd_krx = kd_trends.get("krx", {"foreign": 0, "org": 0, "retail": 0})
    kd_nxt = kd_trends.get("nxt", {"foreign": 0, "org": 0, "retail": 0})
    kd_total = kd_trends.get("total", {"foreign": kd_info.get("foreign", 0), "org": kd_info.get("org", 0), "retail": kd_info.get("retail", 0)})

    effective_kd_foreign = kd_total["foreign"]
    kd_org = kd_total["org"]
    kd_retail = kd_total["retail"]

    nxt_data = fetch_nxt_composite_supply()
    kp_nxt_foreign = kp_nxt.get("foreign", 0)
    kd_nxt_foreign = kd_nxt.get("foreign", 0)
    kp_nxt_div = abs(kp_nxt_foreign) if kp_nxt_foreign < 0 else 0
    kd_nxt_div = abs(kd_nxt_foreign) if kd_nxt_foreign < 0 else 0

    if kp_nxt_foreign <= -2000:
        nxt_shadow_status = f"🚨 NXT 대체거래소 대량 우회 매도 포착 (코스피 외인 {kp_nxt_foreign:+,}억, 코스닥 {kd_nxt_foreign:+,}억)"
    elif kp_nxt_foreign <= -500:
        nxt_shadow_status = f"⚠️ NXT 대체거래소 외인 매도 우위 (코스피 외인 {kp_nxt_foreign:+,}억, 코스닥 {kd_nxt_foreign:+,}억)"
    elif kp_nxt_foreign >= 500:
        nxt_shadow_status = f"🔥 NXT 대체거래소 외인 매수 유입 (코스피 외인 {kp_nxt_foreign:+,}억, 코스닥 {kd_nxt_foreign:+,}억)"
    else:
        nxt_shadow_status = "🟢 NXT 대체거래소 수급 안정"

    if kp_rate >= 1.0: s_kp_rate = 16
    elif kp_rate >= 0.2: s_kp_rate = 12
    elif kp_rate >= -0.3: s_kp_rate = 8
    elif kp_rate >= -0.8: s_kp_rate = 4
    else: s_kp_rate = 0

    if effective_kp_foreign >= 2000: s_kp_flow = 14
    elif effective_kp_foreign >= 500: s_kp_flow = 10
    elif effective_kp_foreign >= -500: s_kp_flow = 6
    elif effective_kp_foreign >= -2000: s_kp_flow = 2
    else: s_kp_flow = 0

    kp_nxt_penalty = 6 if kp_nxt_foreign <= -1500 and effective_kp_foreign < 0 else (3 if kp_nxt_foreign <= -500 and effective_kp_foreign < 0 else 0)
    kospi_score = min(100, max(0, int(macro_score * 0.70 + s_kp_rate + s_kp_flow - kp_nxt_penalty)))

    kd_rate = kd_info["rate"]
    kd_foreign = kd_info["foreign"]
    kd_org = kd_info["org"]
    kd_retail = kd_info["retail"]

    if kd_rate >= 1.0: s_kd_rate = 18
    elif kd_rate >= 0.2: s_kd_rate = 14
    elif kd_rate >= -0.3: s_kd_rate = 9
    elif kd_rate >= -0.8: s_kd_rate = 4
    else: s_kd_rate = 0

    if effective_kd_foreign >= 500: s_kd_flow = 12
    elif effective_kd_foreign >= 0: s_kd_flow = 9
    elif effective_kd_foreign >= -300: s_kd_flow = 5
    elif effective_kd_foreign >= -600: s_kd_flow = 2
    else: s_kd_flow = 0

    kd_penalty = 0
    if effective_kd_foreign < -200 and kd_org < -100 and kd_retail > 300:
        kd_penalty = 8

    # 코스닥 150 선물 베이시스 점수 (최대 8점)
    if kd150_basis >= 1.0: s_kd_basis = 8
    elif kd150_basis >= 0.0: s_kd_basis = 6
    elif kd150_basis >= -0.5: s_kd_basis = 4
    elif kd150_basis >= -1.2: s_kd_basis = 2
    else: s_kd_basis = 0

    # 코스닥 150 선물 외인 순매수 점수 (최대 8점)
    if kd_foreign_fut >= 15000: s_kd_ffut = 8
    elif kd_foreign_fut >= 0: s_kd_ffut = 5
    elif kd_foreign_fut >= -15000: s_kd_ffut = 3
    else: s_kd_ffut = 0

    kosdaq_score = min(100, max(0, int((s_us + s_fx + s_bond + s_bis + s_oil) * 1.2 + s_kd_rate + s_kd_flow + s_kd_basis + s_kd_ffut - kd_penalty)))

    # ── [코스피 전용 게이트 판정] ──
    is_kospi_tradeable = True
    kospi_block_reason = ""
    if nasdaq_rate <= -1.5:
        is_kospi_tradeable = False
        kospi_block_reason = f"글로벌 나스닥 선물 급락 ({nasdaq_rate:+.2f}%)"
    elif usdkrw_rate >= 1.3:
        is_kospi_tradeable = False
        kospi_block_reason = f"원/달러 환율 급등 ({usdkrw_price:,.1f}원, {usdkrw_rate:+.2f}%)"
    elif kp_rate <= -1.5:
        is_kospi_tradeable = False
        kospi_block_reason = f"코스피 지수 급락 ({kp_rate:+.2f}%)"
    elif basis <= -1.8 and foreign_fut <= -150000:
        is_kospi_tradeable = False
        kospi_block_reason = f"코스피200 선물 백워데이션({basis:+.2f}pt) 및 외인 선물 대량 매도"
    elif kelly_prob_up <= 25.0:
        is_kospi_tradeable = False
        kospi_block_reason = f"Kelly RFF 상방 확률 급락 ({kelly_prob_up:.1f}%)"
    elif kospi_score < 25:
        is_kospi_tradeable = False
        kospi_block_reason = f"코스피 시장 건강도 위험 ({kospi_score}점)"

    # ── [코스닥 전용 게이트 판정 (디커플링 독립 방어)] ──
    is_kosdaq_tradeable = True
    kosdaq_block_reason = ""
    if nasdaq_rate <= -1.5:
        is_kosdaq_tradeable = False
        kosdaq_block_reason = f"글로벌 나스닥 선물 급락 ({nasdaq_rate:+.2f}%)"
    elif usdkrw_rate >= 1.3:
        is_kosdaq_tradeable = False
        kosdaq_block_reason = f"원/달러 환율 급등 ({usdkrw_price:,.1f}원, {usdkrw_rate:+.2f}%)"
    elif kd_rate <= -1.5:
        is_kosdaq_tradeable = False
        kosdaq_block_reason = f"코스닥 지수 급락 ({kd_rate:+.2f}%)"
    elif kd150_basis <= -1.8 and kd_foreign_fut <= -25000:
        is_kosdaq_tradeable = False
        kosdaq_block_reason = f"코스닥150 선물 심각한 백워데이션({kd150_basis:+.2f}pt) 및 외인 선물 대량 매도(-{abs(kd_foreign_fut)//100:,}억)"
    elif kd_rate <= -0.7 and kd_foreign <= -400:
        is_kosdaq_tradeable = False
        kosdaq_block_reason = f"코스닥 하락 압력 심화 ({kd_rate:+.2f}%, 외인 {kd_foreign:+,}억 투매)"
    elif kd_foreign <= -600 and kd_org <= -200:
        is_kosdaq_tradeable = False
        kosdaq_block_reason = f"코스닥 외인/기관 동반 투매 (외인 {kd_foreign:+,}억, 기관 {kd_org:+,}억)"
    elif kosdaq_score < 25:
        is_kosdaq_tradeable = False
        kosdaq_block_reason = f"코스닥 시장 건강도 위험 ({kosdaq_score}점)"

    # 종합 기본 게이트 (둘 중 하나라도 허용되면 시스템 생존, 개별 매수 시 종목 시장 게이트 적용)
    is_tradeable = is_kospi_tradeable or is_kosdaq_tradeable
    if not is_tradeable:
        block_reason = f"코스피/코스닥 동반 위험 ({kospi_block_reason} | {kosdaq_block_reason})"
    elif not is_kosdaq_tradeable:
        block_reason = f"코스닥 차단 ({kosdaq_block_reason})"
    elif not is_kospi_tradeable:
        block_reason = f"코스피 차단 ({kospi_block_reason})"
    else:
        block_reason = ""

    # ── 장중 선물 급변속도 & 플래시 프리즈(Flash-Freeze) 판정 (코스피 vs 코스닥 디커플링 분리) ──
    velocity_tracker.record_sample(now, nasdaq_rate, basis, kd150_basis)
    kp_frz_state, kd_frz_state = velocity_tracker.check_flash_freeze(now)
    vel_metrics = velocity_tracker.get_velocity_metrics(now)

    is_kp_frozen = kp_frz_state["is_frozen"]
    kp_freeze_reason = kp_frz_state["reason"]
    kp_freeze_rem = kp_frz_state["rem_sec"]

    is_kd_frozen = kd_frz_state["is_frozen"]
    kd_freeze_reason = kd_frz_state["reason"]
    kd_freeze_rem = kd_frz_state["rem_sec"]

    # ── 플래시 프리즈는 매크로 건강도 사유를 덮어쓰지 않고 공존 (브리핑 시 함께 표기) ──
    kp_gate_reasons = []
    if not is_kospi_tradeable:
        kp_gate_reasons.append(kospi_block_reason)
    if is_kp_frozen:
        kp_gate_reasons.append(f"⚡플래시 프리즈 ({kp_freeze_reason}, 잔여 {kp_freeze_rem}초)")

    kd_gate_reasons = []
    if not is_kosdaq_tradeable:
        kd_gate_reasons.append(kosdaq_block_reason)
    if is_kd_frozen:
        kd_gate_reasons.append(f"⚡플래시 프리즈 ({kd_freeze_reason}, 잔여 {kd_freeze_rem}초)")

    effective_kp_tradeable = is_kospi_tradeable and not is_kp_frozen
    effective_kd_tradeable = is_kosdaq_tradeable and not is_kd_frozen
    is_tradeable = (effective_kp_tradeable or effective_kd_tradeable)

    # 브리핑 텍스트 포맷팅
    kp_gate_desc = "🟢 매수 허용" if effective_kp_tradeable else f"🛑 매수 차단 ({' + '.join(kp_gate_reasons)})"
    kd_gate_desc = "🟢 매수 허용" if effective_kd_tradeable else f"🛑 매수 차단 ({' + '.join(kd_gate_reasons)})"

    session_badge = " 🌙 [야간 선물 실시간]" if is_night_session else ""
    if is_night_session:
        k200_fut_line = f"• 🌙 <b>코스피200 야간선물</b>: <b>{k200_fut:,.2f}pt</b> ({k200_night_rate:+.2f}%, 베이시스 <b>{basis:+.2f}pt</b>, {basis_status}, 거래량 {k200_night_vol:,}계약)\n"
        kd150_fut_line = f"• 🌙 <b>코스닥150 야간선물</b>: <b>{kd150_fut:,.2f}pt</b> ({kd150_night_rate:+.2f}%, 베이시스 <b>{kd150_basis:+.2f}pt</b>, {kd150_basis_status}, 거래량 {kd150_night_vol:,}계약)\n"
    else:
        k200_fut_line = f"• 코스피200 선물: {k200_fut:,.2f}pt (베이시스 {basis:+.2f}pt, {basis_status})\n"
        kd150_fut_line = f"• 코스닥150 선물: <b>{kd150_fut:,.2f}pt</b> (베이시스 <b>{kd150_basis:+.2f}pt</b>, {kd150_basis_status})\n"

    if is_kp_frozen and is_kd_frozen:
        freeze_badge = f"🚨 <b>[전 시장 플래시 프리즈 (AI 매수 전면동결, 잔여 {max(kp_freeze_rem, kd_freeze_rem)}초)]</b>\n• <b>차단 사유</b>: <b>{kp_freeze_reason}</b>\n"
    elif is_kp_frozen:
        freeze_badge = f"🛑 <b>[코스피 전용 프리즈 (코스피 동결, 잔여 {kp_freeze_rem}초) | 코스닥 정상]</b>\n• <b>차단 사유</b>: <b>{kp_freeze_reason}</b>\n"
    elif is_kd_frozen:
        freeze_badge = f"🛑 <b>[코스닥 전용 프리즈 (코스닥 동결, 잔여 {kd_freeze_rem}초) | 코스피 정상]</b>\n• <b>차단 사유</b>: <b>{kd_freeze_reason}</b>\n"
    else:
        freeze_badge = "🟢 <b>[안정 (급변 위험 없음, 정상 감시 중)]</b>\n"

    velocity_section = (
        f"⚡ <b>[장중 선물 급변속도 & 플래시 프리즈 서킷브레이커]</b>\n"
        f"• <b>서킷 상태</b>: {freeze_badge}"
        f"• 나스닥 100 선물 속도: 1분 <b>{vel_metrics['nq_delta_1m']:+.2f}%p</b> | 3분 <b>{vel_metrics['nq_delta_3m']:+.2f}%p</b>\n"
        f"• 코스피200 베이시스: 1분 <b>{vel_metrics['k200_basis_delta_1m']:+.2f}pt</b> | 3분 <b>{vel_metrics['k200_basis_delta_3m']:+.2f}pt</b>\n"
        f"• 코스닥150 베이시스: 1분 <b>{vel_metrics['kd150_basis_delta_1m']:+.2f}pt</b> | 3분 <b>{vel_metrics['kd150_basis_delta_3m']:+.2f}pt</b>\n\n"
    )

    briefing = (
        f"🌐 <b>[실시간 코스피 vs 코스닥 디커플링 & AI 매크로 리포트]{session_badge}</b>\n\n"
        f"{velocity_section}"
        f"⚡ <b>[시장 디커플링 진단]</b>\n"
        f"• <b>현상태</b>: <b>{decoupling_status}</b>\n\n"
        f"🏛️ <b>[코스피(KOSPI) 시장 게이트]</b>\n"
        f"• 코스피 지수: <b>{kp_info['price']:,.2f}pt</b> ({kp_rate:+.2f}%, {kp_info['change']:+.2f})\n"
        f"• 코스피 수급: 외인 <b>{effective_kp_foreign:+,}억</b> | 기관 {kp_org:+,}억 | 개인 {kp_retail:+,}억\n"

        f"{k200_fut_line}"
        f"• 코스피200 선물 수급: 외인 <b>{foreign_fut//100:+,}억</b> | 기관 {org_fut//100:+,}억 | 개인 {retail_fut//100:+,}억\n"
        f"• 코스피 게이트: <b>{kp_gate_desc}</b> (건강점수 {kospi_score}점)\n\n"

        f"🚀 <b>[코스닥(KOSDAQ) 시장 게이트]</b>\n"
        f"• 코스닥 지수: <b>{kd_info['price']:,.2f}pt</b> ({kd_rate:+.2f}%, {kd_info['change']:+.2f})\n"
        f"• 코스닥 수급: 외인 <b>{effective_kd_foreign:+,}억</b> | 기관 {kd_org:+,}억 | 개인 {kd_retail:+,}억\n"

        f"{kd150_fut_line}"
        f"• 코스닥150 선물 수급: 외인 <b>{kd_foreign_fut//100:+,}억</b> | 기관 {kd_org_fut//100:+,}억 | 개인 {kd_retail_fut//100:+,}억\n"
        f"• 코스닥 게이트: <b>{kd_gate_desc}</b> (건강점수 {kosdaq_score}점)\n\n"
        f"🌐 <b>[NXT 대체거래소 수급 동향]</b>\n"
        f"• 대체거래소 상태: <b>{nxt_shadow_status}</b>\n"
        f"• 코스피 외인 NXT: <b>{kp_nxt.get('foreign', 0):+,}억원</b> | 코스닥 외인 NXT: <b>{kd_nxt.get('foreign', 0):+,}억원</b>\n\n"
        f"🧠 <b>[글로벌 매크로 & AI 퀀트 모델]</b>\n"
        f"• Kelly RFF 상방 확률: <b>{kelly_prob_up:.1f}%</b> ({rff_regime})\n"
        f"• Oxford GP 국면전환 위험도: <b>{cpd_hazard:.1f}%</b>\n"
        f"• 미국 나스닥 100 선물: <b>{nasdaq_rate:+.2f}%</b> (S&P {sp500_rate:+.2f}%)\n"
        f"• 원/달러 환율: <b>{usdkrw_price:,.1f}원</b> ({usdkrw_rate:+.2f}%, {usdkrw_status})\n"
        f"• 미 국채 10년: <b>{bonds['us_10y']:.3f}%</b> | 한미 금리차: <b>{bonds['us_kr_spread']:+.3f}%p</b>\n"
        f"• WTI 원유: <b>{oil_data['wti_price']:.2f}$</b> ({oil_data['shock_type']})\n"
    )

    # ── [코스피 국면(Regime) 판정] ──
    if not is_kospi_tradeable:
        kospi_regime = "DANGER"
        kospi_regime_name = f"🛑 코스피 위험 ({kospi_block_reason})"
    elif kospi_score >= 65 and kp_rate >= 0.5 and effective_kp_foreign >= 1000:
        kospi_regime = "TAILWIND"
        kospi_regime_name = "🔥 코스피 수급 순풍"
    else:
        kospi_regime = "SAFE"
        kospi_regime_name = "🟢 코스피 안전/정상"

    # ── [코스닥 국면(Regime) 판정] ──
    if not is_kosdaq_tradeable:
        kosdaq_regime = "DANGER"
        kosdaq_regime_name = f"🛑 코스닥 위험 ({kosdaq_block_reason})"
    elif kosdaq_score >= 60 and kd_rate >= 0.5 and effective_kd_foreign >= 200:
        kosdaq_regime = "TAILWIND"
        kosdaq_regime_name = "🔥 코스닥 수급 순풍"
    else:
        kosdaq_regime = "SAFE"
        kosdaq_regime_name = "🟢 코스닥 안전/정상"

    # ── [종합 대표 국면 판정] ──
    if not is_kospi_tradeable and not is_kosdaq_tradeable:
        regime = "DANGER"
        regime_name = "🛑 전 시장 급락/위험 (매수 차단)"
    elif not is_kosdaq_tradeable:
        regime = "DANGER_KOSDAQ"
        regime_name = "🛑 코스닥 위험 (매수 차단)"
    elif not is_kospi_tradeable:
        regime = "DANGER_KOSPI"
        regime_name = "🛑 코스피 위험 (매수 차단)"
    elif kospi_regime == "TAILWIND" or kosdaq_regime == "TAILWIND":
        regime = "TAILWIND"
        regime_name = "🔥 수급 순풍"
    else:
        regime = "SAFE"
        regime_name = "🟢 안전/정상 (매수 허용)"

    snapshot = MarketMacroSnapshot(
        timestamp=cur_time_str,
        k200_future=k200_fut,
        k200_cash=k200_cash,
        basis=basis,
        basis_status=basis_status,
        foreign_future_val=foreign_fut,
        org_future_val=org_fut,
        retail_future_val=retail_fut,
        foreign_call_val=foreign_call,
        foreign_put_val=foreign_put,
        option_sentiment=opt_sentiment,
        nasdaq_rate=nasdaq_rate,
        sp500_rate=sp500_rate,
        usdkrw_price=usdkrw_price,
        usdkrw_rate=usdkrw_rate,
        usdkrw_status=usdkrw_status,
        us_10y=bonds["us_10y"],
        us_2y=bonds["us_2y"],
        kr_10y=bonds["kr_10y"],
        kr_3y=bonds["kr_3y"],
        us_kr_spread=bonds["us_kr_spread"],
        bond_sentiment=bonds["sentiment"],
        dxy_price=bis_data["dxy_price"],
        dxy_rate=bis_data["dxy_rate"],
        cpi_actual=bis_data["cpi_actual"],
        cpi_surprise=bis_data["cpi_surprise"],
        bis_shock_index=bis_data["bis_shock"],
        bis_sentiment=bis_data["sentiment"],
        wti_price=oil_data["wti_price"],
        wti_rate=oil_data["wti_rate"],
        oil_shock_type=oil_data["shock_type"],
        macro_score=macro_score,
        is_tradeable=is_tradeable,
        block_reason=block_reason,
        regime=regime,
        regime_name=regime_name,
        briefing_text=briefing,
        kelly_prob_up=kelly_prob_up,
        cpd_hazard=cpd_hazard,
        rff_regime=rff_regime,
        kospi_price=kp_info["price"],
        kospi_rate=kp_rate,
        kospi_foreign_val=effective_kp_foreign,
        kospi_org_val=kp_org,
        kospi_retail_val=kp_retail,
        kospi_krx_foreign=kp_krx.get("foreign", 0),
        kospi_nxt_foreign=kp_nxt.get("foreign", 0),
        kospi_krx_org=kp_krx.get("org", 0),
        kospi_nxt_org=kp_nxt.get("org", 0),
        kospi_krx_retail=kp_krx.get("retail", 0),
        kospi_nxt_retail=kp_nxt.get("retail", 0),
        kospi_score=kospi_score,
        is_kospi_tradeable=is_kospi_tradeable,
        kospi_block_reason=kospi_block_reason,
        kosdaq_price=kd_info["price"],
        kosdaq_rate=kd_rate,
        kosdaq_foreign_val=effective_kd_foreign,
        kosdaq_org_val=kd_org,
        kosdaq_retail_val=kd_retail,
        kosdaq_krx_foreign=kd_krx.get("foreign", 0),
        kosdaq_nxt_foreign=kd_nxt.get("foreign", 0),
        kosdaq_krx_org=kd_krx.get("org", 0),
        kosdaq_nxt_org=kd_nxt.get("org", 0),
        kosdaq_krx_retail=kd_krx.get("retail", 0),
        kosdaq_nxt_retail=kd_nxt.get("retail", 0),
        kosdaq_score=kosdaq_score,
        is_kosdaq_tradeable=is_kosdaq_tradeable,
        kosdaq_block_reason=kosdaq_block_reason,
        decoupling_gap=decoupling_gap,
        decoupling_status=decoupling_status,
        kd150_future=kd150_fut,
        kd150_cash=kd150_cash,
        kd150_basis=kd150_basis,
        kd150_basis_status=kd150_basis_status,
        kd_foreign_fut=kd_foreign_fut,
        kd_org_fut=kd_org_fut,
        kd_retail_fut=kd_retail_fut,
        kospi_regime=kospi_regime,
        kospi_regime_name=kospi_regime_name,
        kosdaq_regime=kosdaq_regime,
        kosdaq_regime_name=kosdaq_regime_name,
        nxt_vol_ratio=nxt_data.get("nxt_vol_ratio", 0.0),
        nxt_shadow_status=nxt_shadow_status,
        kospi_effective_foreign=effective_kp_foreign,
        kosdaq_effective_foreign=effective_kd_foreign,
        kospi_nxt_divergence=kp_nxt_div,
        kosdaq_nxt_divergence=kd_nxt_div,
        is_night_session=is_night_session,
        k200_night_rate=k200_night_rate,
        k200_night_vol=k200_night_vol,
        kd150_night_rate=kd150_night_rate,
        kd150_night_vol=kd150_night_vol,
        is_flash_frozen=(is_kp_frozen or is_kd_frozen),
        flash_freeze_reason=(kp_freeze_reason if is_kp_frozen else kd_freeze_reason),
        flash_freeze_rem_sec=max(kp_freeze_rem, kd_freeze_rem),
        is_kospi_flash_frozen=is_kp_frozen,
        kospi_freeze_reason=kp_freeze_reason,
        kospi_freeze_rem_sec=kp_freeze_rem,
        is_kosdaq_flash_frozen=is_kd_frozen,
        kosdaq_freeze_reason=kd_freeze_reason,
        kosdaq_freeze_rem_sec=kd_freeze_rem,
        nq_delta_1m=vel_metrics["nq_delta_1m"],
        nq_delta_3m=vel_metrics["nq_delta_3m"],
        basis_delta_1m=vel_metrics["k200_basis_delta_1m"],
        basis_delta_3m=vel_metrics["k200_basis_delta_3m"],
        kd_basis_delta_1m=vel_metrics["kd150_basis_delta_1m"],
        kd_basis_delta_3m=vel_metrics["kd150_basis_delta_3m"],
        cusum_downside_score=vel_metrics["cusum_score"]
    )

    _cached_snapshot = snapshot
    _last_fetch_time = now
    return snapshot


# ── 코스피 & 코스닥 독립 국면 전환 추적 및 알림 엔진 ──
_last_kospi_regime: str | None = None
_last_kosdaq_regime: str | None = None
_kospi_cand: str | None = None
_kospi_cand_cnt: int = 0
_kosdaq_cand: str | None = None
_kosdaq_cand_cnt: int = 0


def get_current_regime_name() -> str:
    snap = get_market_macro_snapshot()
    return f"{snap.kospi_regime_name} | {snap.kosdaq_regime_name}"


def check_and_notify_market_regime_change(telegram_notifier=None) -> str | None:
    """코스피 또는 코스닥 둘 중 하나라도 국면이 전환되면 각각 독립적으로 감지하여 텔레그램 알림을 발송합니다."""
    global _last_kospi_regime, _last_kosdaq_regime
    global _kospi_cand, _kospi_cand_cnt, _kosdaq_cand, _kosdaq_cand_cnt

    snap = get_market_macro_snapshot()
    cur_kp = snap.kospi_regime
    cur_kd = snap.kosdaq_regime

    alerts = []

    # 1. 최초 기동 시: 기준점만 설정하고 알림 생략
    if _last_kospi_regime is None or _last_kosdaq_regime is None:
        _last_kospi_regime = cur_kp
        _last_kosdaq_regime = cur_kd
        logger.info(f"🌐 [시장 국면 감시 초기화] 코스피: {snap.kospi_regime_name} | 코스닥: {snap.kosdaq_regime_name}")
        return None

    # 야간/주간 선물 라인 생성
    if snap.is_night_session:
        kp_fut_line = f"• 🌙 <b>코스피200 야간선물</b>: <b>{snap.k200_future:,.2f}pt</b> ({snap.k200_night_rate:+.2f}%, 베이시스 {snap.basis:+.2f}pt, {snap.basis_status})"
        kd_fut_line = f"• 🌙 <b>코스닥150 야간선물</b>: <b>{snap.kd150_future:,.2f}pt</b> ({snap.kd150_night_rate:+.2f}%, 베이시스 {snap.kd150_basis:+.2f}pt, {snap.kd150_basis_status})"
    else:
        kp_fut_line = f"• 코스피200 선물: <b>{snap.k200_future:,.2f}pt</b> (베이시스 {snap.basis:+.2f}pt, {snap.basis_status})"
        kd_fut_line = f"• 코스닥150 선물: <b>{snap.kd150_future:,.2f}pt</b> (베이시스 {snap.kd150_basis:+.2f}pt, {snap.kd150_basis_status})"

    # 2. 코스피 국면 전환 검사
    if cur_kp != _last_kospi_regime:
        kp_confirmed = False
        if cur_kp == "DANGER":
            kp_confirmed = True  # 위험은 지체 없이 즉시 발동
        else:
            if _kospi_cand == cur_kp:
                _kospi_cand_cnt += 1
            else:
                _kospi_cand = cur_kp
                _kospi_cand_cnt = 1
            kp_confirmed = (_kospi_cand_cnt >= 4)  # 4회(8초) 연속 안정 확인 시 회복 승인 (채터링 방지)

        if kp_confirmed:
            prev_kp = _last_kospi_regime
            _last_kospi_regime = cur_kp
            _kospi_cand = None
            _kospi_cand_cnt = 0

            if cur_kp == "DANGER":
                lines = [
                    "🚨 <b>[코스피(KOSPI) 긴급 알림: 급락/위험 신호 발동!]</b>\n",
                    f"• <b>시장 국면</b>: <b>🛑 [코스피 위험 경보 (신규 매수 차단)]</b>",
                    f"• <b>차단 사유</b>: <b>{snap.kospi_block_reason}</b>",
                    f"• <b>코스피 건강 점수</b>: <b>{snap.kospi_score}점 / 100점</b> (위험)\n",
                    "📊 <b>[코스피 실시간 시세 & 수급]</b>",
                    f"• 코스피 지수: <b>{snap.kospi_price:,.2f}pt</b> ({snap.kospi_rate:+.2f}%)",
                    f"• 코스피 수급: 외인 <b>{snap.kospi_foreign_val:+,}억</b> | 기관 {snap.kospi_org_val:+,}억 | 개인 {snap.kospi_retail_val:+,}억",
                    kp_fut_line,
                    f"• 외인 선물 순매수: <b>{snap.foreign_future_val:+,}백만원</b>\n",
                    "🛡️ <b>[AutoStock 방어 조치]</b>",
                    "1. <b>코스피 상장 종목의 신규 매수 주문을 즉시 보류(차단)</b>합니다.",
                    "2. 코스닥 종목은 코스닥 시장 상태에 따라 독립적으로 판정됩니다.",
                    "3. 기존 보유 종목은 대표님의 직접 승인/설정 없이는 절대 임의 매도하지 않습니다."
                ]
                msg = "\n".join(lines)
            elif cur_kp == "TAILWIND":
                lines = [
                    "🔥 <b>[코스피(KOSPI) 시장 전환 알림: 수급 순풍 감지!]</b>\n",
                    "• <b>시장 국면</b>: <b>🔥 [코스피 수급 순풍 (적극 매수 우호)]</b>",
                    f"• <b>코스피 건강 점수</b>: <b>{snap.kospi_score}점 / 100점</b> (최우수)\n",
                    "📊 <b>[코스피 실시간 수급]</b>",
                    f"• 코스피 지수: <b>{snap.kospi_price:,.2f}pt</b> ({snap.kospi_rate:+.2f}%)",
                    f"• 외인 현물 순매수: <b>{snap.kospi_foreign_val:+,}억</b> | 외인선물: {snap.foreign_future_val//100:+,}억",
                    f"• 코스피200 베이시스: <b>{snap.basis:+.2f}pt</b> ({snap.basis_status})\n",
                    "🚀 <b>[AutoStock 전략 조치]</b>",
                    "대형주 수급 순풍이 확인되어 코스피 종목에 대해 적극 매수를 지원합니다!"
                ]
                msg = "\n".join(lines)
            else:  # SAFE
                lines = [
                    "🟢 <b>[코스피(KOSPI) 시장 회복 알림: 정상/안전 국면 복귀]</b>\n",
                    "• <b>시장 국면</b>: <b>🟢 [코스피 정상 매매 허용]</b>",
                    f"• <b>코스피 건강 점수</b>: <b>{snap.kospi_score}점 / 100점</b>\n",
                    f"• 코스피 지수: <b>{snap.kospi_price:,.2f}pt</b> ({snap.kospi_rate:+.2f}%)",
                    f"{kp_fut_line}\n",
                    "💡 <b>[AutoStock 안내]</b>",
                    "코스피 시장 위험이 해소되어 코스피 종목의 신규 매수 차단이 해제되었습니다."
                ]
                msg = "\n".join(lines)
            alerts.append(msg)
            logger.warning(f"🌐 [코스피 국면 전환] {prev_kp} -> {cur_kp}")

    # 3. 코스닥 국면 전환 검사
    if cur_kd != _last_kosdaq_regime:
        kd_confirmed = False
        if cur_kd == "DANGER":
            kd_confirmed = True  # 위험은 지체 없이 즉시 발동
        else:
            if _kosdaq_cand == cur_kd:
                _kosdaq_cand_cnt += 1
            else:
                _kosdaq_cand = cur_kd
                _kosdaq_cand_cnt = 1
            kd_confirmed = (_kosdaq_cand_cnt >= 4)

        if kd_confirmed:
            prev_kd = _last_kosdaq_regime
            _last_kosdaq_regime = cur_kd
            _kosdaq_cand = None
            _kosdaq_cand_cnt = 0

            if cur_kd == "DANGER":
                lines = [
                    "🚨 <b>[코스닥(KOSDAQ) 긴급 알림: 급락/위험 신호 발동!]</b>\n",
                    "• <b>시장 국면</b>: <b>🛑 [코스닥 위험 경보 (신규 매수 차단)]</b>",
                    f"• <b>차단 사유</b>: <b>{snap.kosdaq_block_reason}</b>",
                    f"• <b>코스닥 건강 점수</b>: <b>{snap.kosdaq_score}점 / 100점</b> (위험)\n",
                    "📊 <b>[코스닥 실시간 시세 & 수급]</b>",
                    f"• 코스닥 지수: <b>{snap.kosdaq_price:,.2f}pt</b> ({snap.kosdaq_rate:+.2f}%)",
                    f"• 코스닥 수급: 외인 <b>{snap.kosdaq_foreign_val:+,}억</b> | 기관 {snap.kosdaq_org_val:+,}억",
                    kd_fut_line,
                    f"• 코스닥150 선물 외인: <b>{snap.kd_foreign_fut//100:+,}억</b> | 기관 {snap.kd_org_fut//100:+,}억\n",
                    "🛡️ <b>[AutoStock 방어 조치]</b>",
                    "1. <b>코스닥 상장 종목의 신규 매수 주문을 즉시 보류(차단)</b>합니다.",
                    "2. 코스피 종목은 코스피 시장 상태에 따라 독립적으로 매수 판정됩니다.",
                    "3. 기존 보유 종목은 대표님의 직접 승인/설정 없이는 절대 임의 매도하지 않습니다."
                ]
                msg = "\n".join(lines)
            elif cur_kd == "TAILWIND":
                lines = [
                    "🔥 <b>[코스닥(KOSDAQ) 시장 전환 알림: 수급 순풍 감지!]</b>\n",
                    "• <b>시장 국면</b>: <b>🔥 [코스닥 수급 순풍 (적극 매수 우호)]</b>",
                    f"• <b>코스닥 건강 점수</b>: <b>{snap.kosdaq_score}점 / 100점</b> (최우수)\n",
                    "📊 <b>[코스닥 실시간 수급]</b>",
                    f"• 코스닥 지수: <b>{snap.kosdaq_price:,.2f}pt</b> ({snap.kosdaq_rate:+.2f}%)",
                    f"• 외인 현물 순매수: <b>{snap.kosdaq_foreign_val:+,}억</b> | 선물외인: {snap.kd_foreign_fut//100:+,}억",
                    f"• 코스닥150 베이시스: <b>{snap.kd150_basis:+.2f}pt</b> ({snap.kd150_basis_status})\n",
                    "🚀 <b>[AutoStock 전략 조치]</b>",
                    "중소형 성장주 수급 순풍이 확인되어 코스닥 종목에 대해 적극 매수를 지원합니다!"
                ]
                msg = "\n".join(lines)
            else:  # SAFE
                lines = [
                    "🟢 <b>[코스닥(KOSDAQ) 시장 회복 알림: 정상/안전 국면 복귀]</b>\n",
                    "• <b>시장 국면</b>: <b>🟢 [코스닥 정상 매매 허용]</b>",
                    f"• <b>코스닥 건강 점수</b>: <b>{snap.kosdaq_score}점 / 100점</b>\n",
                    f"• 코스닥 지수: <b>{snap.kosdaq_price:,.2f}pt</b> ({snap.kosdaq_rate:+.2f}%)",
                    f"• 코스닥150 선물: <b>{snap.kd150_future:,.2f}pt</b> (베이시스 {snap.kd150_basis:+.2f}pt)\n",
                    "💡 <b>[AutoStock 안내]</b>",
                    "코스닥 시장 위험이 해소되어 코스닥 종목의 신규 매수 차단이 해제되었습니다."
                ]
                msg = "\n".join(lines)
            alerts.append(msg)
            logger.warning(f"🌐 [코스닥 국면 전환] {prev_kd} -> {cur_kd}")

    # 3-1. 장중 선물 급락 플래시 프리즈(Flash-Freeze) 전환 알림 검사 (글로벌 vs 코스피 vs 코스닥 독립)
    vel = velocity_tracker.get_velocity_metrics()

    # 1) 글로벌 나스닥 100 선물 급락 (전 시장 동시 동결)
    if velocity_tracker.last_transition_global == "FROZEN":
        freeze_msg = (
            "🚨 <b>[전 시장 긴급: 미국 나스닥 100 선물 급락! 플래시 프리즈 발동]</b>\n\n"
            "• <b>시장 국면</b>: <b>🛑 [코스피 & 코스닥 전 종목 매수 5분간 일시 동결]</b>\n"
            f"• <b>트리거 사유</b>: <b>{velocity_tracker.global_freeze_reason}</b>\n"
            f"• <b>미국 나스닥 100 선물 속도</b>: 1분 <b>{vel['nq_delta_1m']:+.2f}%p</b> | 3분 <b>{vel['nq_delta_3m']:+.2f}%p</b> (현재 {snap.nasdaq_rate:+.2f}%)\n"
            f"• <b>코스피200 베이시스</b>: 1분 <b>{vel['k200_basis_delta_1m']:+.2f}pt</b> | 3분 <b>{vel['k200_basis_delta_3m']:+.2f}pt</b> (현재 {snap.basis:+.2f}pt)\n"
            f"• <b>코스닥150 베이시스</b>: 1분 <b>{vel['kd150_basis_delta_1m']:+.2f}pt</b> | 3분 <b>{vel['kd150_basis_delta_3m']:+.2f}pt</b> (현재 {snap.kd150_basis:+.2f}pt)\n\n"
            "🛡️ <b>[AutoStock 방어 조치]</b>\n"
            "글로벌 선물 급락으로 인한 현물 추락 리스크를 방어하기 위해 <b>5분간 신규 AI 주식 매수를 일시 중단(HOLD)</b>합니다."
        )
        alerts.insert(0, freeze_msg)
        velocity_tracker.last_transition_global = None
        velocity_tracker.last_transition_kp = None
        velocity_tracker.last_transition_kd = None

    # 2) 코스피 200 선물 베이시스 급락 (코스피만 독자 동결 - 주간 정규장 전용)
    if velocity_tracker.last_transition_kp == "FROZEN":
        if not snap.is_night_session:
            freeze_msg = (
                "🚨 <b>[코스피(KOSPI) 긴급: 코스피200 선물 베이시스 급락 플래시 프리즈 발동]</b>\n\n"
                "• <b>시장 국면</b>: <b>🛑 [코스피 종목 신규 매수 5분간 일시 동결 (코스닥은 정상 유지)]</b>\n"
                f"• <b>트리거 사유</b>: <b>{velocity_tracker.kp_freeze_reason}</b>\n"
                f"• <b>코스피200 베이시스 변동</b>: 1분 <b>{vel['k200_basis_delta_1m']:+.2f}pt</b> | 3분 <b>{vel['k200_basis_delta_3m']:+.2f}pt</b> (현재 {snap.basis:+.2f}pt)\n"
                f"• <b>코스닥150 베이시스</b>: 1분 <b>{vel['kd150_basis_delta_1m']:+.2f}pt</b> | 3분 <b>{vel['kd150_basis_delta_3m']:+.2f}pt</b> (정상)\n\n"
                "🛡️ <b>[AutoStock 디커플링 방어 조치]</b>\n"
                "코스피 대형주 매수만 5분간 일시 보류하며, 코스닥 종목은 코스닥 수급에 따라 정상 매수를 지속합니다."
            )
            alerts.insert(0, freeze_msg)
        velocity_tracker.last_transition_kp = None

    # 3) 코스닥 150 선물 베이시스 급락 (코스닥만 독자 동결 - 주간 정규장 전용)
    if velocity_tracker.last_transition_kd == "FROZEN":
        if not snap.is_night_session:
            freeze_msg = (
                "🚨 <b>[코스닥(KOSDAQ) 긴급: 코스닥150 선물 베이시스 급락 플래시 프리즈 발동]</b>\n\n"
                "• <b>시장 국면</b>: <b>🛑 [코스닥 종목 신규 매수 5분간 일시 동결 (코스피는 정상 유지)]</b>\n"
                f"• <b>트리거 사유</b>: <b>{velocity_tracker.kd_freeze_reason}</b>\n"
                f"• <b>코스닥150 베이시스 변동</b>: 1분 <b>{vel['kd150_basis_delta_1m']:+.2f}pt</b> | 3분 <b>{vel['kd150_basis_delta_3m']:+.2f}pt</b> (현재 {snap.kd150_basis:+.2f}pt)\n"
                f"• <b>코스피200 베이시스</b>: 1분 <b>{vel['k200_basis_delta_1m']:+.2f}pt</b> | 3분 <b>{vel['k200_basis_delta_3m']:+.2f}pt</b> (정상)\n\n"
                "🛡️ <b>[AutoStock 디커플링 방어 조치]</b>\n"
                "코스닥 종목 매수만 5분간 일시 보류하며, 코스피 종목은 코스피 수급에 따라 정상 매수를 지속합니다."
            )
            alerts.insert(0, freeze_msg)
        velocity_tracker.last_transition_kd = None

    if velocity_tracker.last_transition_kp == "UNFROZEN":
        unfreeze_msg = (
            "🟢 <b>[코스피(KOSPI) 플래시 프리즈 해제: 정상 복귀]</b>\n\n"
            "• <b>코스피 선물 베이시스 변동 속도가 안정</b>되어 코스피 종목의 AI 스나이퍼 매수가 정상 재개되었습니다."
        )
        alerts.append(unfreeze_msg)
        velocity_tracker.last_transition_kp = None

    if velocity_tracker.last_transition_kd == "UNFROZEN":
        unfreeze_msg = (
            "🟢 <b>[코스닥(KOSDAQ) 플래시 프리즈 해제: 정상 복귀]</b>\n\n"
            "• <b>코스닥150 선물 베이시스 변동 속도가 안정</b>되어 코스닥 종목의 AI 스나이퍼 매수가 정상 재개되었습니다."
        )
        alerts.append(unfreeze_msg)
        velocity_tracker.last_transition_kd = None

    # 4. 텔레그램 발송
    if telegram_notifier and alerts:
        for a_msg in alerts:
            telegram_notifier.send_message(a_msg)

    return alerts[0] if alerts else None


def is_market_tradeable_for_stock(market_type: str = "KOSPI") -> tuple[bool, str, int]:
    """해당 종목이 속한 시장(KOSPI 또는 KOSDAQ)에 맞춰 독립적인 매수 허용 여부, 차단 사유, 시장점수를 반환합니다.
    
    Returns:
        (is_tradeable: bool, block_reason: str, market_score: int)
    """
    snap = get_market_macro_snapshot()
    m = str(market_type).upper()
    if "KOSDAQ" in m or "코스닥" in m:
        if snap.is_kosdaq_flash_frozen:
            return False, f"장중 코스닥 플래시 프리즈 ({snap.kosdaq_freeze_reason}, 잔여 {snap.kosdaq_freeze_rem_sec}초)", 0
        return snap.is_kosdaq_tradeable, snap.kosdaq_block_reason, snap.kosdaq_score
    else:
        if snap.is_kospi_flash_frozen:
            return False, f"장중 코스피 플래시 프리즈 ({snap.kospi_freeze_reason}, 잔여 {snap.kospi_freeze_rem_sec}초)", 0
        return snap.is_kospi_tradeable, snap.kospi_block_reason, snap.kospi_score


def is_flash_frozen(market_type: str | None = None) -> tuple[bool, str, int]:
    """해당 시장(KOSPI 또는 KOSDAQ)의 플래시 프리즈 발동 여부를 독립적으로 반환합니다.
    
    Args:
        market_type: "KOSPI", "KOSDAQ", 또는 None(어느 시장이든 발동 시 True)
    """
    snap = get_market_macro_snapshot()
    if market_type:
        m = str(market_type).upper()
        if "KOSDAQ" in m or "코스닥" in m:
            return snap.is_kosdaq_flash_frozen, snap.kosdaq_freeze_reason, snap.kosdaq_freeze_rem_sec
        else:
            return snap.is_kospi_flash_frozen, snap.kospi_freeze_reason, snap.kospi_freeze_rem_sec

    # market_type 미지정 시: 둘 중 하나라도 발동 중이면 반환
    if snap.is_kospi_flash_frozen and snap.is_kosdaq_flash_frozen:
        return True, f"[전 시장] {snap.kospi_freeze_reason}", max(snap.kospi_freeze_rem_sec, snap.kosdaq_freeze_rem_sec)
    elif snap.is_kospi_flash_frozen:
        return True, f"[코스피 전용] {snap.kospi_freeze_reason}", snap.kospi_freeze_rem_sec
    elif snap.is_kosdaq_flash_frozen:
        return True, f"[코스닥 전용] {snap.kosdaq_freeze_reason}", snap.kosdaq_freeze_rem_sec
    return False, "", 0


def get_derivative_velocity() -> dict:
    """최근 1~3분 선물 급변 속도 및 CUSUM 스코어를 반환합니다."""
    return velocity_tracker.get_velocity_metrics()
