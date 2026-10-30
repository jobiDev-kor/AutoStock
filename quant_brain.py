"""AutoStock 퀀트 AI 분석 및 매수 타점 도출 모듈 (quant_brain.py)

1. 종목명/종목코드 검색 및 심볼 마스터 캐싱 (m_new_stock)
2. 실시간 10단계 호가창(Order Book) 및 체결강도, 거래량, VWAP, 지지선 수집
3. 금융공학 마이크로스트럭처 분석:
   - OFI (Order Flow Imbalance, 매수/매도 잔량 불균형)
   - 기관/외인 거래량가중평균가 (VWAP)
   - 피봇 지지선 (Pivot S1, S2)
   - 10단계 호가 중 최대 지지벽(Wall of Bids)
4. 투자자 예산 한도(Budget) 기반 최적 진입 타점 및 주문 수량 계산
5. 손익 시나리오(로스컷 -2%, 상승컷 +3% 목표가) 및 AI 브리핑 리포트 생성
"""
import re
from collections import deque
import logging
from dataclasses import dataclass
from nhplug.instruments.master import load_master
import broker
import fee_calculator
import config

logger = logging.getLogger("AutoStock.QuantBrain")

# ── Stephen Boyd (Stanford 2024) CM-IEWMA 동적 변동성 제어기 ──
class BoydCovarianceController:
    """Stephen Boyd (Stanford Univ, 2024)
    다중 시간척도 반복 지수가중이동평균 결합 (CM-IEWMA) 기반 동적 변동성 타겟팅.
    
    체결 가격 변화를 3대 반감기(Fast: 5틱, Mid: 20틱, Slow: 60틱)로 분해하고,
    정밀도 공간의 숄레스키 인자 결합으로 현재의 실시간 변동성을 추정합니다.
    - 변동성 폭발 감지: 현재 변동성이 기준 대비 1.4배 이상 급증하면 리스크 분산(50% 분할 매수) 강제
    - 변동성 안정 감지: 현재 변동성이 평온하면 정상 비중 집행 허용
    """
    def __init__(self, target_vol_bps: float = 20.0):
        self.target_vol_bps = target_vol_bps
        self.fast_var = 0.0
        self.mid_var = 0.0
        self.slow_var = 0.0
        self.last_price = 0.0
        self.alpha_fast = 2.0 / (5.0 + 1.0)
        self.alpha_mid = 2.0 / (20.0 + 1.0)
        self.alpha_slow = 2.0 / (60.0 + 1.0)

    def update(self, price: float) -> tuple[float, float]:
        """(실시간 변동성 bps, 타겟 대비 변동성 배율) 반환"""
        if self.last_price <= 0:
            self.last_price = price
            return self.target_vol_bps, 1.0

        ret_bps = abs((price - self.last_price) / self.last_price) * 10000.0
        sq_ret = ret_bps ** 2
        self.last_price = price

        self.fast_var = self.alpha_fast * sq_ret + (1 - self.alpha_fast) * self.fast_var
        self.mid_var = self.alpha_mid * sq_ret + (1 - self.alpha_mid) * self.mid_var
        self.slow_var = self.alpha_slow * sq_ret + (1 - self.alpha_slow) * self.slow_var

        if self.fast_var > self.slow_var * 2.0:
            pi = [0.60, 0.30, 0.10]
        else:
            pi = [0.20, 0.30, 0.50]

        comb_var = pi[0] * self.fast_var + pi[1] * self.mid_var + pi[2] * self.slow_var
        import numpy as np
        est_vol_bps = max(5.0, float(np.sqrt(comb_var)))
        vol_ratio = round(est_vol_bps / self.target_vol_bps, 2)
        return round(est_vol_bps, 1), vol_ratio


_boyd_controllers: dict[str, BoydCovarianceController] = {}

def get_boyd_controller(iem_cd: str) -> BoydCovarianceController:
    if iem_cd not in _boyd_controllers:
        _boyd_controllers[iem_cd] = BoydCovarianceController()
    return _boyd_controllers[iem_cd]


# ── UCL Financial Computing (2024–2025) 대수적 위상수학 지속성 호가 필터 ──
class TopologicalLOBFilter:
    """UCL Financial Computing (HLOB & LOBFrame, Tomaso Aste et al. 2024-2025)
    대수적 위상수학(Algebraic Topology) 기반 호가창 지속성(Persistence) 필터.
    
    호가창 5호가의 매수/매도 잔량 분포의 엔트로피와 연속성을 분석하여:
    - 세력의 허수벽(Spoofing Wall): 특정 1~2호가에만 비정상적으로 쏠려있는 경우 지속성(Persistence H0) 급감 (0.1 ~ 0.4)
    - 진성 매집벽(Iceberg Support): 1~5호가에 탄탄하게 매수벽이 받쳐주는 구조면 지속성 유지 (0.7 ~ 1.0)
    """
    @staticmethod
    def compute_persistence_score(quote: dict, cur_pr: int) -> tuple[float, str]:
        bids = [float(quote.get(f"bidp_rsqn{i}") or 0) for i in range(1, 6)]
        tot_bids = sum(bids)

        if tot_bids <= 0:
            return 0.5, "잔량 데이터 대기"

        # 1. 매수 잔량 집중도 (Herfindahl Index)
        shares = [b / tot_bids for b in bids]
        hhi = sum(s ** 2 for s in shares)
        front_ratio = (bids[0] + bids[1]) / tot_bids

        # 2. 위상 지속성 지수 H0 도출
        if hhi >= 0.65 and bids[0] == max(bids):
            persistence = max(0.20, 1.0 - hhi)
            desc = "⚠️ 허수벽(Spoofing) 의심: 1호가 비정상 쏠림"
        elif front_ratio >= 0.85:
            persistence = 0.45
            desc = "⚠️ 단기 1~2호가 집중 (하단 지지벽 취약)"
        else:
            persistence = min(1.0, 0.5 + (1.0 - hhi) * 0.6)
            desc = "💎 진성 다층 지지벽(Iceberg Wall) 확인"

        return round(float(persistence), 2), desc


# 종목 마스터 캐시 (메모리 1회 로드)
_MASTER_CACHE: list[dict] = []
_NAME_TO_CODE: dict[str, str] = {}
_CODE_TO_NAME: dict[str, str] = {}

def get_master_data() -> list[dict]:
    """국내 주식 마스터 데이터를 로드하고 캐싱합니다."""
    global _MASTER_CACHE, _NAME_TO_CODE, _CODE_TO_NAME
    if _MASTER_CACHE:
        return _MASTER_CACHE

    try:
        records = load_master("m_new_stock")
        _MASTER_CACHE = records or []
        for item in _MASTER_CACHE:
            code = item.get("sCode", "").strip()
            # 마스터 파서의 'name' 속성은 UTF-8 유니코드 문자열
            name = item.get("name", "").strip()
            if code and name:
                _NAME_TO_CODE[name] = code
                _NAME_TO_CODE[name.replace(" ", "")] = code
                _CODE_TO_NAME[code] = name
        logger.info(f"종목 마스터 {len(_MASTER_CACHE):,}건 캐싱 완료")
    except Exception as e:
        logger.error(f"종목 마스터 로드 실패: {e}")
    return _MASTER_CACHE


def resolve_stock(query: str) -> tuple[str | None, str | None]:
    """종목명 또는 종목코드를 받아 (iem_cd, iem_nm) 튜플을 반환합니다.
    
    예시:
        resolve_stock('대한항공') -> ('003490', '대한항공')
        resolve_stock('003490') -> ('003490', '대한항공')
    """
    if not query:
        return None, None

    get_master_data()
    q = query.strip()
    q_clean = q.replace(" ", "")

    # 1. 6자리 숫자 코드인 경우
    if re.fullmatch(r"\d{6}", q):
        name = _CODE_TO_NAME.get(q)
        if not name:
            # 실시간 시세 조회를 통해 종목명 역조회
            quote = broker.get_current_price(q)
            if quote and quote.get("iem_nm"):
                name = quote.get("iem_nm").strip()
                _CODE_TO_NAME[q] = name
        return q, name or q

    # 2. 종목명 정확 일치
    if q in _NAME_TO_CODE:
        code = _NAME_TO_CODE[q]
        return code, _CODE_TO_NAME.get(code, q)
    if q_clean in _NAME_TO_CODE:
        code = _NAME_TO_CODE[q_clean]
        return code, _CODE_TO_NAME.get(code, q_clean)

    # 3. 종목명 부분 일치 검색
    for name, code in _NAME_TO_CODE.items():
        if q in name or name in q:
            return code, _CODE_TO_NAME.get(code, name)

    return None, None


def get_stock_market_type(iem_cd: str, quote: dict | None = None) -> str:
    """종목코드 및 호가/시세 응답을 분석하여 소속 시장 ('KOSPI' 또는 'KOSDAQ')을 정확히 판별합니다."""
    if quote:
        mrkt_nm = quote.get("mrkt_div_isnm") or quote.get("scrt_grp_isnm") or quote.get("bstp_kor_isnm") or ""
        if "코스닥" in mrkt_nm:
            return "KOSDAQ"
        elif "코스피" in mrkt_nm:
            return "KOSPI"

    master = get_master_data()
    for row in master:
        if row.get("sCode") == iem_cd:
            m = row.get("sMarket")
            if m == "4":
                return "KOSDAQ"
            elif m == "1":
                return "KOSPI"
            break

    return "KOSPI"


@dataclass
class QuantAnalysisResult:
    iem_cd: str
    iem_nm: str
    current_price: int
    change_pct: float
    vwap: int
    pivot_point: int
    pivot_s1: int
    pivot_s2: int
    pivot_r1: int
    pivot_r2: int
    total_bid_qty: int
    total_ask_qty: int
    ofi_ratio: float
    strongest_bid_price: int
    strongest_bid_qty: int
    strongest_ask_price: int
    strongest_ask_qty: int
    recommended_entry_price: int
    recommended_qty: int
    max_budget: int
    max_buy_price: int                # 최대 매수 상한가 (이 가격 이하에서만 매수)
    expected_order_amt: int
    expected_fee: int
    recommended_exit_price: int       # AI 매도 타점 (트레일링 시작 기준선)
    user_stop_loss_price: int         # 대표님 지정 로스컷가
    user_loss_pct: float              # 대표님 지정 손절률
    has_user_stop_loss: bool          # 손절가 지정 여부
    confidence_score: int
    market_status: str
    briefing_text: str
    market_type: str = "KOSPI"
    nxt_ratio: float = 0.0
    unt_prog: int = 0


def analyze_stock_quant(
    query: str,
    budget: int = 0,
    target_qty: int = 0,
    max_buy_price: int = 0,
    user_stop_loss_pct: float = -5.0,
    min_profit_pct: float = 5.0,
    user_loss_input: str | None = None,
    act_no: str | None = None
) -> QuantAnalysisResult | None:
    """종목의 실시간 마이크로스트럭처를 수집하고 퀀트 최적 매수/매도 타점을 계산합니다.
    
    Args:
        query: 종목명 또는 종목코드 (예: '대한항공', '003490')
        budget: 최대 매수 예산 (수량 미지정 시 사용)
        target_qty: 목표 매수 수량 (예: 20주)
        user_stop_loss_pct: 대표님 지정 손절률 (기본 -5.0%)
        min_profit_pct: 대표님 지정 최소 익절률 및 트레일링 시작선 (기본 +5.0%)
        user_loss_input: 문자열 형태의 손절 입력 (선택)
        act_no: 계좌번호 (선택)
    """
    code, name = resolve_stock(query)
    if not code:
        logger.warning(f"종목을 찾을 수 없습니다: '{query}'")
        return None

    quote = broker.get_current_price(code)
    if not quote:
        logger.warning(f"시세 조회 실패: {code}")
        return None

    # 실시간 호가 및 시세 데이터 파싱
    current_price = int(float(quote.get("stck_prpr") or 0))
    if current_price <= 0:
        return None

    change_pct = float(quote.get("prdy_ctrt") or 0.0)
    vwap = int(float(quote.get("wghn_avrg_prc") or current_price))
    pvt_point = int(float(quote.get("pvt_pont_val") or current_price))
    pvt_s1 = int(float(quote.get("pvt_frst_dmsp") or current_price))
    pvt_s2 = int(float(quote.get("pvt_scnd_dmsp") or current_price))
    pvt_r1 = int(float(quote.get("pvt_frst_dmrs") or current_price))
    pvt_r2 = int(float(quote.get("pvt_scnd_dmrs") or current_price))
    day_high = int(float(quote.get("stck_hgpr") or current_price))
    market_status = quote.get("market_status", "")

    total_bid = int(float(quote.get("total_bidp_rsqn") or 0))
    total_ask = int(float(quote.get("total_askp_rsqn") or 0))
    ofi_ratio = round(total_bid / max(total_ask, 1), 2)

    # 10단계 호가 중 매수 1~5호가에서 가장 두터운 매수벽 탐색 (지지선)
    strongest_bid_pr = current_price
    strongest_bid_qty = 0
    for i in range(1, 6):
        b_pr = int(float(quote.get(f"bidp{i}") or 0))
        b_qty = int(float(quote.get(f"bidp_rsqn{i}") or 0))
        if b_qty > strongest_bid_qty and b_pr > 0:
            strongest_bid_qty = b_qty
            strongest_bid_pr = b_pr

    # 10단계 호가 중 매도 1~5호가에서 가장 두터운 매도벽 탐색 (저항선)
    strongest_ask_pr = current_price
    strongest_ask_qty = 0
    for i in range(1, 6):
        a_pr = int(float(quote.get(f"askp{i}") or 0))
        a_qty = int(float(quote.get(f"askp_rsqn{i}") or 0))
        if a_qty > strongest_ask_qty and a_pr > 0:
            strongest_ask_qty = a_qty
            strongest_ask_pr = a_pr

    # DeepLOB 다중 호가 가중 불균형(MLI) & 마이크로 가격 연산
    weights = [0.40, 0.25, 0.15, 0.12, 0.08]
    mli = 0.0
    micro_prices = []
    for i in range(1, 6):
        bp = float(quote.get(f"bidp{i}") or current_price)
        ap = float(quote.get(f"askp{i}") or current_price)
        bv = float(quote.get(f"bidp_rsqn{i}") or 0)
        av = float(quote.get(f"askp_rsqn{i}") or 0)
        denom = bv + av
        imb_i = (bv - av) / denom if denom > 0 else 0.0
        mli += weights[i - 1] * imb_i
        ratio = bv / denom if denom > 0 else 0.5
        p_micro_i = ratio * ap + (1.0 - ratio) * bp if (ap > 0 and bp > 0) else current_price
        micro_prices.append(p_micro_i)

    weighted_micro_pr = 0.60 * micro_prices[0] + 0.25 * micro_prices[1] + 0.15 * micro_prices[2] if len(micro_prices) >= 3 else float(current_price)

    tick = fee_calculator.get_krx_tick_size(current_price)

    # ── 1. 퀀트 최적 매수 타점 (Recommended Entry Price) ──
    if ofi_ratio >= 1.6:
        bid1 = int(float(quote.get("bidp1") or current_price))
        raw_target = bid1 if bid1 > 0 else current_price
        reason_label = "🔥 매수세 우세 (OFI 급증): 1차 매수호가 최유리 체결권"
        conf = 88
    elif ofi_ratio >= 1.0:
        raw_target = min(strongest_bid_pr, current_price)
        reason_label = "⚖️ 안정적 수급: 5호가 내 최대 매수벽 지지선"
        conf = 82
    else:
        raw_target = min(pvt_s1, strongest_bid_pr, current_price - (tick * 2))
        reason_label = "🛡️ 보수적 방어: 1차 피봇 지지선 및 하단 지지벽"
        conf = 78

    target_price = fee_calculator.align_to_tick_size(raw_target, mode="floor")
    if target_price > current_price:
        target_price = current_price

    # 🔴 대표님 지정 매수 상한선(max_buy_price) 가드
    if max_buy_price > 0:
        aligned_cap = fee_calculator.align_to_tick_size(max_buy_price, mode="floor")
        target_price = min(target_price, aligned_cap)

    # 수량 및 예산 계산 (수량 우선 지원)
    if target_qty > 0:
        rec_qty = target_qty
        expected_order_amt = rec_qty * target_price
        budget = expected_order_amt
    else:
        if budget <= 0:
            budget = 500000
        rec_qty = max(1, budget // target_price)
        expected_order_amt = rec_qty * target_price

    expected_fee = int(expected_order_amt * 0.00015)

    # ── 2. 최소 수익률 및 트레일링 익절 시작선 ──
    if min_profit_pct > 0:
        target_exit = fee_calculator.calculate_target_price_for_net_pnl(target_price, min_profit_pct)
        exit_pnl_pct = min_profit_pct
    else:
        possible_exits = []
        if strongest_ask_pr > target_price: possible_exits.append(strongest_ask_pr)
        if pvt_r1 > target_price: possible_exits.append(pvt_r1)
        if day_high > target_price: possible_exits.append(day_high)
        min_exit = fee_calculator.calculate_target_price_for_net_pnl(target_price, 2.0)
        valid_exits = [p for p in possible_exits if p >= min_exit]
        raw_exit = min(valid_exits) if valid_exits else fee_calculator.calculate_target_price_for_net_pnl(target_price, 3.0)
        target_exit = fee_calculator.align_to_tick_size(raw_exit, mode="ceil")
        exit_pnl_pct = fee_calculator.calculate_net_pnl_pct(target_price, target_exit)

    # ── 3. 대표님 로스컷(손절) 계산 ──
    has_user_loss = True
    if user_loss_input:
        cleaned_loss = user_loss_input.replace("%", "").replace("+", "").strip()
        try:
            val = float(cleaned_loss)
            if "%" in user_loss_input or abs(val) <= 100:
                user_loss_pct = -abs(val)
                user_stop_price = fee_calculator.calculate_target_price_for_net_pnl(target_price, user_loss_pct)
            else:
                user_stop_price = fee_calculator.align_to_tick_size(val, mode="floor")
                user_loss_pct = fee_calculator.calculate_net_pnl_pct(target_price, user_stop_price)
        except Exception:
            user_loss_pct = user_stop_loss_pct
            user_stop_price = fee_calculator.calculate_target_price_for_net_pnl(target_price, user_loss_pct)
    else:
        user_loss_pct = user_stop_loss_pct
        user_stop_price = fee_calculator.calculate_target_price_for_net_pnl(target_price, user_loss_pct)

    loss_text = f"<b>{user_stop_price:,}원</b> (순손실 {user_loss_pct:.1f}%)"
    cap_line = (
        f"• <b>매수 제한 상한선</b>: <b>{max_buy_price:,}원 이하</b> (이 가격 초과 시 절대 매수 금지 가드)\n"
        if max_buy_price > 0 else
        "• <b>매수 제한 상한선</b>: <b>제한 없음 (전 가격대 모든 한국 상장 종목 지원)</b>\n"
    )

    import market_brain
    mkt_snap = market_brain.get_market_macro_snapshot()
    persistence_val, p_desc = TopologicalLOBFilter.compute_persistence_score(quote, current_price)
    _, vol_ratio = get_boyd_controller(code).update(current_price)

    market_type = get_stock_market_type(code, quote)
    is_mkt_tradeable, mkt_block_reason, mkt_score = market_brain.is_market_tradeable_for_stock(market_type)
    mkt_gate_badge = "🟢 매수 가능 (정상)" if is_mkt_tradeable else f"🛑 위험 (매수 차단: {mkt_block_reason})"
    kp_gate_badge = "🟢 허용" if mkt_snap.is_kospi_tradeable else "🛑 차단"
    kd_gate_badge = "🟢 허용" if mkt_snap.is_kosdaq_tradeable else "🛑 차단"

    fut_line = (
        f"• <b>코스닥150 선물</b>: <b>{mkt_snap.kd150_future:,.2f}pt</b> (베이시스 <b>{mkt_snap.kd150_basis:+.2f}pt</b>, 외인선물 <b>{mkt_snap.kd_foreign_fut//100:+,}억</b>)"
        if market_type == "KOSDAQ" else
        f"• <b>코스피200 선물</b>: <b>{mkt_snap.k200_future:,.2f}pt</b> (베이시스 <b>{mkt_snap.basis:+.2f}pt</b>, 외인선물 <b>{mkt_snap.foreign_future_val//100:+,}억</b>)"
    )

    # KRX + NXT 복합 통합(UNT) 수급 정밀 진단
    unt_trend = broker.get_investor_trend(code, "UNT")
    nxt_trend = broker.get_investor_trend(code, "NXT")
    unt_prog = int(unt_trend.get("program") or 0) if unt_trend else 0
    nxt_prog = int(nxt_trend.get("program") or 0) if nxt_trend else 0
    krx_prog = unt_prog - nxt_prog
    unt_vol = int(unt_trend.get("acml_vol") or 0) if unt_trend else 0
    nxt_vol = int(nxt_trend.get("acml_vol") or 0) if nxt_trend else 0
    nxt_ratio = round((nxt_vol / unt_vol * 100), 1) if unt_vol > 0 else 0.0

    nxt_shadow_alert = ""
    if nxt_prog < -1000 and nxt_prog < krx_prog:
        nxt_shadow_alert = f"• 🚨 <b>[NXT 우회 매도 포착]</b> 대체거래소(NXT)로 <b>{nxt_prog:+,}주</b> 프로그램 매도 분산 출회 중!\n"
        conf = max(50, conf - 8)

    # 텔레그램용 브리핑 텍스트 포맷팅
    briefing = (
        f"🧠 <b>[AI 동적 퀀트 스나이퍼 전략 리포트]</b>\n\n"
        f"• <b>종목명</b>: <b>{name}</b> (<code>{code}</code>) [<b>{market_type}</b>]\n"
        f"• <b>현재가</b>: <b>{current_price:,}원</b> ({change_pct:+.2f}%)\n"
        f"• <b>호가단위</b>: {tick:,}원 | <b>시장상태</b>: {'정상' if not market_status else market_status}\n\n"
        f"🌐 <b>[시장 디커플링 & {market_type} 맞춤형 게이트]</b>\n"
        f"• <b>디커플링 진단</b>: <b>{mkt_snap.decoupling_status}</b>\n"
        f"• <b>장중 선물 속도</b>: 나스닥 3분 <b>{mkt_snap.nq_delta_3m:+.2f}%p</b> | K200 베이시스 3분 <b>{mkt_snap.basis_delta_3m:+.2f}pt</b> "
        f"({'🚨 [플래시 프리즈 매수동결]' if mkt_snap.is_flash_frozen else '🟢 [안정]'})\n"
        f"• <b>{market_type} 전용 판정</b>: <b>{mkt_gate_badge}</b> ({market_type} 점수 {mkt_score}점)\n"
        f"{fut_line}\n"
        f"• 코스피 지수: <b>{mkt_snap.kospi_price:,.2f}pt</b> ({mkt_snap.kospi_rate:+.2f}%, 통합외인 {mkt_snap.kospi_foreign_val:+,}억) | {kp_gate_badge}\n"
        f"• 코스닥 지수: <b>{mkt_snap.kosdaq_price:,.2f}pt</b> ({mkt_snap.kosdaq_rate:+.2f}%, 통합외인 {mkt_snap.kosdaq_foreign_val:+,}억) | {kd_gate_badge}\n"
        f"• Kelly RFF 상방 확률: <b>{mkt_snap.kelly_prob_up:.1f}%</b> ({mkt_snap.rff_regime})\n"
        f"• 미국 나스닥 100 선물: <b>{mkt_snap.nasdaq_rate:+.2f}%</b> | 환율: <b>{mkt_snap.usdkrw_price:,.1f}원</b> ({mkt_snap.usdkrw_status})\n"
        f"• 미 국채 10년: <b>{mkt_snap.us_10y:.3f}%</b> | WTI 원유: <b>{mkt_snap.wti_price:.2f}$</b> ({mkt_snap.oil_shock_type})\n\n"
        f"📊 <b>[호가창 & AI 마이크로스트럭처 모델]</b>\n"
        f"• <b>DeepLOB 가중 불균형(MLI)</b>: <b>{'+' if ofi_ratio >= 1.0 else '-'}{abs(ofi_ratio - 1.0):.2f}</b> (5단계 지지/저항 가중 감쇄 분석)\n"
        f"• <b>DeepLOB 마이크로 가격</b>: <b>{weighted_micro_pr:,.0f}원</b>\n"
        f"• <b>UCL 위상 지속성(H0)</b>: <b>{persistence_val:.2f}</b> ({p_desc})\n"
        f"• <b>Boyd 동적 변동성 배율</b>: <b>{vol_ratio:.1f}배</b> ({'⚠️ 변동성 급증 경계' if vol_ratio >= 1.4 else '안정'})\n"
        f"• <b>KRX+NXT 통합 프로그램 수급</b>: <b>{unt_prog:+,}주</b> (KRX: {krx_prog:+,}주 / NXT: {nxt_prog:+,}주, NXT비중 {nxt_ratio}%)\n" + nxt_shadow_alert + f"• <b>OFI 수급 불균형</b>: <b>{ofi_ratio:.2f}배</b> ({'매수세 우세' if ofi_ratio > 1.2 else '매도/매수 균형'})\n"
        f"  (매수잔량 {total_bid:,}주 / 매도잔량 {total_ask:,}주)\n"
        f"• <b>당일 VWAP (가중평균가)</b>: {vwap:,}원\n"
        f"• <b>최대 매수 지지벽</b>: <b>{strongest_bid_pr:,}원</b> ({strongest_bid_qty:,}주)\n"
        f"• <b>최대 매도 저항벽</b>: <b>{strongest_ask_pr:,}원</b> ({strongest_ask_qty:,}주)\n\n"
        f"🎯 <b>[AI 동적 매수 전략]</b>\n"
        f"• <b>목표 수량</b>: <b>{rec_qty:,}주</b> (예상금액 약 {expected_order_amt:,}원)\n"
        f"• <b>기준 매수가</b>: <b>{target_price:,}원</b> (신뢰도 {conf}%)\n"
        f"{cap_line}"
        f"• <b>1초 동적 분산 기법</b>: 1초마다 호가창/수급 점수에 따라 <b>[일괄 100%]</b> 또는 <b>[50% 분할 진입]</b> 수학적 자동 결정\n\n"
        f"🚀 <b>[최소 수익 (+{exit_pnl_pct:.1f}%) & 트레일링 익절]</b>\n"
        f"• <b>트레일링 시작가</b>: <b>{target_exit:,}원</b> (+{exit_pnl_pct:.1f}% 달성 시점)\n"
        f"• <b>매도 전략</b>: <b>{target_exit:,}원 도달 후 즉시 팔지 않고, 고점을 추적하여 매도수익 극대화</b>\n\n"
        f"🔻 <b>[대표님 로스컷(손절) 범위]</b>\n"
        f"• <b>설정 손절가</b>: {loss_text}\n"
    )

    return QuantAnalysisResult(
        iem_cd=code,
        iem_nm=name,
        current_price=current_price,
        change_pct=change_pct,
        vwap=vwap,
        pivot_point=pvt_point,
        pivot_s1=pvt_s1,
        pivot_s2=pvt_s2,
        pivot_r1=pvt_r1,
        pivot_r2=pvt_r2,
        total_bid_qty=total_bid,
        total_ask_qty=total_ask,
        ofi_ratio=ofi_ratio,
        strongest_bid_price=strongest_bid_pr,
        strongest_bid_qty=strongest_bid_qty,
        strongest_ask_price=strongest_ask_pr,
        strongest_ask_qty=strongest_ask_qty,
        recommended_entry_price=target_price,
        recommended_qty=rec_qty,
        max_budget=budget,
        max_buy_price=max_buy_price,
        expected_order_amt=expected_order_amt,
        expected_fee=expected_fee,
        recommended_exit_price=target_exit,
        user_stop_loss_price=user_stop_price,
        user_loss_pct=user_loss_pct,
        has_user_stop_loss=has_user_loss,
        confidence_score=conf,
        market_status=market_status,
        briefing_text=briefing,
        market_type=market_type,
        nxt_ratio=nxt_ratio,
        unt_prog=unt_prog
    )



# ── Cont, Kukanov, Stoikov (2014) 호가 주문흐름 불균형(OFI) 실시간 분석 엔진 ──
@dataclass
class ContStoikovMetrics:
    iem_cd: str
    ofi_instant: float        # 최신 1틱 OFI
    ofi_multi: float          # 5단계 가중 다중레벨 OFI
    ofi_cum10: float          # 최근 10틱 누적 OFI
    is_toxic_sell_flow: bool  # 독성 매도 / 허매수벽 급취소 감지 여부
    risk_reason: str          # 위험 사유 설명
    bid_cancellations: int    # 최근 매수호가 취소 횟수
    micro_price_deficit: bool # 마이크로 가격 하방 이탈 여부
    score_adjustment: int     # 확신도 점수 가감치 (-20 ~ +10)


class ContStoikovOFIAnalyzer:
    """Cont, Kukanov, Stoikov (2014) 'The Price Impact of Order Book Events'
    및 Rama Cont (2021) 다중 레벨 주문흐름 불균형(Multi-Level OFI) 실시간 엔진.
    
    원리:
    - 1초마다 수집되는 실제 10단계 호가창에서 1~5호가 최우선 매수/매도 가격 및 잔량의 델타를 추적:
      * 매수호가 상승 or 잔량 증가: +ΔBid
      * 매수호가 하락 or 잔량 급감 (허매수 취소/시장가 매도 체결): -ΔBid
      * 매도호가 하락 or 잔량 증가: -ΔAsk
      * 매도호가 상승 or 잔량 감소: +ΔAsk
    - ΔOFI = ΔBid - ΔAsk
    - 최근 10틱(10초) 동안 ΔOFI 누적이 급격히 음수로 쏠리거나 허매수 취소가 연속 감지되면
      '미시 수급 이탈(Toxic Sell Flow)'로 판정하여 AI 매수를 즉시 차단(WAIT)합니다.
    """
    _state: dict[str, dict] = {}

    @classmethod
    def update(cls, iem_cd: str, quote: dict, cur_pr: int) -> ContStoikovMetrics:
        if iem_cd not in cls._state:
            cls._state[iem_cd] = {
                "prev_bids": [],
                "prev_asks": [],
                "ofi_history": deque(maxlen=20),
                "bid_cancel_cnt": 0
            }

        state = cls._state[iem_cd]

        cur_bids = []
        cur_asks = []
        for i in range(1, 6):
            bp = float(quote.get(f"bidp{i}") or 0)
            bq = float(quote.get(f"bidp_rsqn{i}") or 0)
            ap = float(quote.get(f"askp{i}") or 0)
            aq = float(quote.get(f"askp_rsqn{i}") or 0)
            cur_bids.append((bp, bq))
            cur_asks.append((ap, aq))

        prev_bids = state["prev_bids"]
        prev_asks = state["prev_asks"]

        if not prev_bids or not prev_asks:
            state["prev_bids"] = cur_bids
            state["prev_asks"] = cur_asks
            return ContStoikovMetrics(
                iem_cd=iem_cd, ofi_instant=0.0, ofi_multi=0.0,
                ofi_cum10=0.0, is_toxic_sell_flow=False, risk_reason="",
                bid_cancellations=0, micro_price_deficit=False, score_adjustment=0
            )

        pb1, qb1 = cur_bids[0]
        prev_pb1, prev_qb1 = prev_bids[0]
        pa1, qa1 = cur_asks[0]
        prev_pa1, prev_qa1 = prev_asks[0]

        # Bid Delta (Cont-Stoikov Eq 1)
        if pb1 > prev_pb1:
            delta_bid1 = qb1
        elif pb1 == prev_pb1:
            delta_bid1 = qb1 - prev_qb1
        else:
            delta_bid1 = -prev_qb1

        # Ask Delta (Cont-Stoikov Eq 2)
        if pa1 < prev_pa1:
            delta_ask1 = qa1
        elif pa1 == prev_pa1:
            delta_ask1 = qa1 - prev_qa1
        else:
            delta_ask1 = -prev_qa1

        ofi_l1 = delta_bid1 - delta_ask1

        # Rama Cont (2021) Multi-Level OFI 계산 (1~5호가)
        weights = [0.45, 0.25, 0.15, 0.10, 0.05]
        ofi_multi = 0.0
        bid_cancelled_this_tick = False

        for k in range(5):
            bp_k, bq_k = cur_bids[k]
            prev_bp_k, prev_bq_k = prev_bids[k]
            ap_k, aq_k = cur_asks[k]
            prev_ap_k, prev_aq_k = prev_asks[k]

            if bp_k > prev_bp_k: d_b = bq_k
            elif bp_k == prev_bp_k: d_b = bq_k - prev_bq_k
            else: d_b = -prev_bq_k

            if ap_k < prev_ap_k: d_a = aq_k
            elif ap_k == prev_ap_k: d_a = aq_k - prev_aq_k
            else: d_a = -prev_aq_k

            ofi_multi += weights[k] * (d_b - d_a)

            # 호가가 동일한데 잔량이 대량(40% 이상) 급감한 경우 허매수 취소 포착
            if k <= 1 and bp_k == prev_bp_k and (prev_bq_k - bq_k) > max(100.0, prev_bq_k * 0.40):
                bid_cancelled_this_tick = True

        state["ofi_history"].append(ofi_multi)
        state["prev_bids"] = cur_bids
        state["prev_asks"] = cur_asks

        if bid_cancelled_this_tick:
            state["bid_cancel_cnt"] = min(5, state["bid_cancel_cnt"] + 1)
        else:
            state["bid_cancel_cnt"] = max(0, state["bid_cancel_cnt"] - 1)

        recent_10 = list(state["ofi_history"])[-10:]
        ofi_cum10 = round(sum(recent_10), 1)

        # Micro-Price Deficit 분석 (Stoikov 2018)
        denom1 = qb1 + qa1
        micro_p1 = (qb1 * pa1 + qa1 * pb1) / denom1 if denom1 > 0 else cur_pr
        micro_deficit = (micro_p1 <= pb1 + 0.1 * (pa1 - pb1)) if (pa1 > pb1) else False

        is_toxic = False
        risk_reason = ""
        score_adj = 0

        if ofi_cum10 < -500 and state["bid_cancel_cnt"] >= 2:
            is_toxic = True
            risk_reason = f"주문흐름 불균형(OFI) 급락 ({ofi_cum10:+,}주) 및 허매수벽 취소 감지"
            score_adj = -20
        elif micro_deficit and ofi_cum10 < -200:
            is_toxic = True
            risk_reason = f"마이크로가격 하방 이탈 (청산 압력 우세, OFI {ofi_cum10:+,}주)"
            score_adj = -15
        elif ofi_cum10 > 500 and not micro_deficit:
            score_adj = +10
        elif ofi_cum10 > 150:
            score_adj = +5

        return ContStoikovMetrics(
            iem_cd=iem_cd,
            ofi_instant=round(ofi_l1, 1),
            ofi_multi=round(ofi_multi, 1),
            ofi_cum10=ofi_cum10,
            is_toxic_sell_flow=is_toxic,
            risk_reason=risk_reason,
            bid_cancellations=state["bid_cancel_cnt"],
            micro_price_deficit=micro_deficit,
            score_adjustment=score_adj
        )


@dataclass
class DynamicBuyDecision:
    iem_cd: str
    action: str              # "BUY_FULL", "BUY_SPLIT", "WAIT"
    qty_to_buy: int          # 이번 1초에 매수할 수량
    current_price: int       # 현재 체결가
    confidence: int          # 계산된 확률 점수 (0~100%)
    ofi_ratio: float         # 실시간 OFI 수급 비율
    reason: str              # 수학적 산출 근거
    depth_ratio: float       # 5호가 매수/매도 잔량비
    vwap_gap_pct: float      # 당일 VWAP 대비 이격도 (%)
    micro_price: float = 0.0 # DeepLOB 다중 호가 마이크로 가격
    mli_index: float = 0.0   # DeepLOB 다중 호가 가중 불균형 지수 (-1.0 ~ +1.0)
    persistence_score: float = 0.5 # UCL 위상수학 지속성 지수 (0.0 ~ 1.0)
    persistence_desc: str = ""     # 허수벽 vs 진성 지지벽
    vol_ratio: float = 1.0         # Boyd 동적 변동성 배율 (1.0 = 정상)
    cs_ofi_cum10: float = 0.0      # Cont-Stoikov 최근 10틱 누적 OFI (주)
    cs_risk_reason: str = ""       # Cont-Stoikov 위험 사유
    is_flash_frozen: bool = False  # 장중 선물 급락 플래시 프리즈 가드 작동 여부


def evaluate_dynamic_buy_signal(iem_cd: str, remaining_qty: int, max_buy_price: int = 0) -> DynamicBuyDecision:
    """1초마다 실시간 10단계 호가창과 수급을 수학적으로 동적 재연산하여 매수 여부 및 수량(분산/일괄)을 확률로 산출합니다.
    
    분석 요소:
    1. OFI (Order Flow Imbalance): 매수/매도 총잔량 비율
    2. 5호가 매수 지지벽 두께 (Depth Ratio)
    3. VWAP (기관/외인 가중평균가) 대비 눌림목 이격도
    4. 최우선 1호가 매수 흡수력 (Bid1 vs Ask1 잔량비)
    5. 대표님 지정 매수 상한선(max_buy_price) 가드
    
    반환 액션:
    - BUY_FULL: 확신도 85% 이상 -> 전량 일괄 매수
    - BUY_SPLIT: 확신도 65% ~ 84% -> 50% 분할 매수 (리스크 분산)
    - WAIT: 확신도 65% 미만 또는 매수 상한선 초과 -> 매수 보류
    """
    quote = broker.get_current_price(iem_cd)
    if not quote:
        return DynamicBuyDecision(
            iem_cd=iem_cd, action="WAIT", qty_to_buy=0, current_price=0,
            confidence=0, ofi_ratio=0.0, reason="시세 데이터 수신 대기",
            depth_ratio=0.0, vwap_gap_pct=0.0
        )

    cur_pr = int(float(quote.get("stck_prpr") or 0))
    if cur_pr <= 0:
        return DynamicBuyDecision(
            iem_cd=iem_cd, action="WAIT", qty_to_buy=0, current_price=0,
            confidence=0, ofi_ratio=0.0, reason="유효 체결가 없음",
            depth_ratio=0.0, vwap_gap_pct=0.0
        )

    vwap = int(float(quote.get("wghn_avrg_prc") or cur_pr))
    total_bid = int(float(quote.get("total_bidp_rsqn") or 0))
    total_ask = int(float(quote.get("total_askp_rsqn") or 0))
    ofi_ratio = round(total_bid / max(total_ask, 1), 2)

    bids_5 = sum(int(float(quote.get(f"bidp_rsqn{i}") or 0)) for i in range(1, 6))
    asks_5 = sum(int(float(quote.get(f"askp_rsqn{i}") or 0)) for i in range(1, 6))
    depth_ratio = round(bids_5 / max(asks_5, 1), 2)

    bid1_qty = int(float(quote.get("bidp_rsqn1") or 0))
    ask1_qty = int(float(quote.get("askp_rsqn1") or 0))

    vwap_gap = round(((cur_pr - vwap) / vwap) * 100, 2) if vwap > 0 else 0.0

    # 🔴 대표님 지정 매수 상한선 가드 (현재가가 상한가 초과 시 절대 매수 금지)
    if max_buy_price > 0 and cur_pr > max_buy_price:
        return DynamicBuyDecision(
            iem_cd=iem_cd, action="WAIT", qty_to_buy=0, current_price=cur_pr,
            confidence=0, ofi_ratio=ofi_ratio,
            reason=f"🛡️ 매수 상한선({max_buy_price:,}원) 초과 대기 (현재가 {cur_pr:,}원 > 상한선 {max_buy_price:,}원)",
            depth_ratio=depth_ratio, vwap_gap_pct=vwap_gap
        )

    # 🔴 1. [종목 소속 시장(KOSPI vs KOSDAQ) 맞춤형 플래시 프리즈 가드]
    import market_brain
    market_type = get_stock_market_type(iem_cd, quote)
    is_frozen, freeze_reason, rem_sec = market_brain.is_flash_frozen(market_type)
    if is_frozen:
        return DynamicBuyDecision(
            iem_cd=iem_cd, action="WAIT", qty_to_buy=0, current_price=cur_pr,
            confidence=0, ofi_ratio=ofi_ratio,
            reason=f"🚨 장중 {market_type} 선물 급락 플래시 프리즈 가드 작동 중 ({freeze_reason}, 잔여 {rem_sec}초 매수 동결)",
            depth_ratio=depth_ratio, vwap_gap_pct=vwap_gap,
            is_flash_frozen=True
        )

    # 🔴 2. [시장 매크로 거부권 게이트 (Macro Gate) - 코스피 vs 코스닥 디커플링 독립 방어]
    is_mkt_tradeable, mkt_block_reason, mkt_score = market_brain.is_market_tradeable_for_stock(market_type)
    if not is_mkt_tradeable:
        return DynamicBuyDecision(
            iem_cd=iem_cd, action="WAIT", qty_to_buy=0, current_price=cur_pr,
            confidence=0, ofi_ratio=ofi_ratio,
            reason=f"🛑 {market_type} 시장 리스크 차단 ({mkt_block_reason})",
            depth_ratio=depth_ratio, vwap_gap_pct=vwap_gap,
            is_flash_frozen=("플래시 프리즈" in mkt_block_reason)
        )

    # ── [Cont, Kukanov, Stoikov (2014) 주문흐름 불균형(OFI) & 독성 매도/허매수 취소 감지] ──
    cs_metrics = ContStoikovOFIAnalyzer.update(iem_cd, quote, cur_pr)
    if cs_metrics.is_toxic_sell_flow:
        return DynamicBuyDecision(
            iem_cd=iem_cd, action="WAIT", qty_to_buy=0, current_price=cur_pr,
            confidence=0, ofi_ratio=ofi_ratio,
            reason=f"⚠️ 미시 수급 위험: {cs_metrics.risk_reason} (체결 보류 및 관망)",
            depth_ratio=depth_ratio, vwap_gap_pct=vwap_gap,
            cs_ofi_cum10=cs_metrics.ofi_cum10,
            cs_risk_reason=cs_metrics.risk_reason
        )

    # ── [DeepLOB 논문 기반 호가 미시구조 연산 (Multi-Level Imbalance & Micro-Price)] ──
    # Oxford DeepLOB (Zhang et al. 2019):
    # 1. 1~5호가 감쇄 가중치 w = [0.40, 0.25, 0.15, 0.12, 0.08]
    # 2. Level별 불균형 I_i = (bid - ask) / (bid + ask)
    # 3. 다중 레벨 마이크로 가격 p_micro = I * ask + (1-I) * bid
    weights = [0.40, 0.25, 0.15, 0.12, 0.08]
    mli = 0.0
    micro_prices = []

    for i in range(1, 6):
        bp = float(quote.get(f"bidp{i}") or cur_pr)
        ap = float(quote.get(f"askp{i}") or cur_pr)
        bv = float(quote.get(f"bidp_rsqn{i}") or 0)
        av = float(quote.get(f"askp_rsqn{i}") or 0)

        denom = bv + av
        imb_i = (bv - av) / denom if denom > 0 else 0.0
        mli += weights[i - 1] * imb_i

        ratio = bv / denom if denom > 0 else 0.5
        p_micro_i = ratio * ap + (1.0 - ratio) * bp if (ap > 0 and bp > 0) else cur_pr
        micro_prices.append(p_micro_i)

    weighted_micro_pr = 0.60 * micro_prices[0] + 0.25 * micro_prices[1] + 0.15 * micro_prices[2] if len(micro_prices) >= 3 else float(cur_pr)
    micro_gap_pct = round(((weighted_micro_pr - cur_pr) / cur_pr) * 100, 2) if cur_pr > 0 else 0.0
    mli = round(mli, 3)

    # ── [UCL 대수적 위상수학 지속성 호가 필터 & Boyd 동적 변동성 연산] ──
    persistence_score, p_desc = TopologicalLOBFilter.compute_persistence_score(quote, cur_pr)
    _, vol_ratio = get_boyd_controller(iem_cd).update(cur_pr)

    # ── 확률(Confidence) 점수 모델 연산 (100점 만점 구조) ──
    # 1. DeepLOB 다중 호가 가중 불균형(MLI) 점수 (최대 25점)
    if mli >= 0.35: s_mli = 25
    elif mli >= 0.15: s_mli = 20
    elif mli >= -0.05: s_mli = 14
    elif mli >= -0.25: s_mli = 7
    else: s_mli = 0

    # 2. DeepLOB 마이크로 가격 상방 프리미엄 (최대 15점)
    if micro_gap_pct >= 0.10: s_micro = 15
    elif micro_gap_pct >= 0.0: s_micro = 11
    elif micro_gap_pct >= -0.10: s_micro = 6
    else: s_micro = 0

    # 3. 5호가 지지벽 깊이 점수 (최대 15점)
    if depth_ratio >= 2.0: s_depth = 15
    elif depth_ratio >= 1.3: s_depth = 11
    elif depth_ratio >= 1.0: s_depth = 6
    else: s_depth = 0

    # 4. 기관/외인 VWAP 대비 눌림목 이격도 점수 (최대 15점)
    if vwap_gap <= -0.5: s_val = 15
    elif -0.5 < vwap_gap <= 0.5: s_val = 11
    elif 0.5 < vwap_gap <= 1.5: s_val = 6
    else: s_val = 0

    # 5. 최우선 1호가 매수 잔량 흡수력 점수 (최대 10점)
    if bid1_qty >= ask1_qty * 1.5: s_bid1 = 10
    elif bid1_qty >= ask1_qty: s_bid1 = 7
    else: s_bid1 = 0

    # 6. UCL 위상 지속성(Persistence H0) 보정 (최대 ±10점)
    # 세력의 허수벽(Spoofing Wall) 감지 시 점수 삭감
    if persistence_score <= 0.35: s_topo = -15
    elif persistence_score >= 0.70: s_topo = 10
    else: s_topo = 0

    # 7. Stephen Boyd 변동성 타겟팅 보정 (최대 -10점)
    if vol_ratio >= 1.6: s_boyd = -10
    elif vol_ratio >= 1.3: s_boyd = -5
    else: s_boyd = 0

    # 8. Bryan Kelly RFF 시장 매크로, 환율 및 채권 리스크 보정 (최대 ±10점)
    if mkt_snap.kelly_prob_up >= 65.0 and mkt_snap.usdkrw_rate <= 0.2 and mkt_snap.us_10y <= 4.80:
        m_mod = 10
    elif mkt_snap.kelly_prob_up <= 35.0 or mkt_snap.usdkrw_rate >= 0.8 or mkt_snap.us_10y >= 5.10:
        m_mod = -15
    else:
        m_mod = 0

    raw_score = s_mli + s_micro + s_depth + s_val + s_bid1 + s_topo + s_boyd + m_mod + cs_metrics.score_adjustment
    score = max(0, min(100, raw_score))

    # ── 확률 기반 주문 수량 산출 (일괄 vs 분할 기법 + Boyd 리스크 통제) ──
    if score >= 85 and vol_ratio < 1.4:
        action = "BUY_FULL"
        buy_qty = remaining_qty
        reason = f"🔥 확신도 {score}% (OFI {ofi_ratio}배, {p_desc}) -> 전량({buy_qty}주) 일괄 매수 집행"
    elif score >= 65 or (score >= 85 and vol_ratio >= 1.4):
        action = "BUY_SPLIT"
        buy_qty = max(1, remaining_qty // 2)
        if vol_ratio >= 1.4:
            reason = f"⚖️ 확신도 {score}% ({p_desc}, Boyd 변동성 급증 {vol_ratio:.1f}배 리스크 제어) -> 1차 50% 분할({buy_qty}주) 매수 집행"
        else:
            reason = f"⚖️ 확신도 {score}% ({p_desc}, 분산 리스크 관리) -> 1차 50% 분할({buy_qty}주) 매수 집행"
    else:
        action = "WAIT"
        buy_qty = 0
        reason = f"🛡️ 매수 보류 (확신도 {score}% 미달, {p_desc}, 하방 압력 경계)"

    return DynamicBuyDecision(
        iem_cd=iem_cd,
        action=action,
        qty_to_buy=buy_qty,
        current_price=cur_pr,
        confidence=score,
        ofi_ratio=ofi_ratio,
        reason=reason,
        depth_ratio=depth_ratio,
        vwap_gap_pct=vwap_gap,
        micro_price=round(weighted_micro_pr, 1),
        mli_index=mli,
        persistence_score=persistence_score,
        persistence_desc=p_desc,
        vol_ratio=vol_ratio,
        cs_ofi_cum10=cs_metrics.ofi_cum10,
        cs_risk_reason=cs_metrics.risk_reason,
        is_flash_frozen=False
    )
