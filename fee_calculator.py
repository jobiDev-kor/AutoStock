"""유관기관 제비용, 증권사 수수료 및 거래세를 반영한 실질 평단가 및 목표가 계산 모듈"""

# 기본 세율 및 수수료율 설정 (사용자 지정: 매수/매도 수수료 각 0.015%)
DEFAULT_BUY_FEE_RATE = 0.00015       # 매수 수수료율 0.015%
DEFAULT_SELL_FEE_RATE = 0.00015      # 매도 수수료율 0.015%
DEFAULT_SELL_TAX_RATE = 0.0018       # 증권거래세율 0.18%

# 왕복 총 제비용율: 0.21% (0.015% + 0.015% + 0.18%)
TOTAL_ROUNDTRIP_FEE_RATE = DEFAULT_BUY_FEE_RATE + DEFAULT_SELL_FEE_RATE + DEFAULT_SELL_TAX_RATE


def get_effective_buy_price(buy_price: float, buy_fee_rate: float = DEFAULT_BUY_FEE_RATE) -> float:
    """매수 수수료를 반영한 1주당 실제 실질 매입단가 반환."""
    return buy_price * (1.0 + buy_fee_rate)


def calculate_net_pnl_pct(
    buy_price: float,
    sell_price: float,
    buy_fee_rate: float = DEFAULT_BUY_FEE_RATE,
    sell_fee_rate: float = DEFAULT_SELL_FEE_RATE,
    sell_tax_rate: float = DEFAULT_SELL_TAX_RATE
) -> float:
    """매수/매도 수수료 및 거래세를 모두 차감한 후의 순수익률(%) 계산."""
    if buy_price <= 0:
        return 0.0

    cost_per_share = buy_price * (1.0 + buy_fee_rate)
    net_proceeds_per_share = sell_price * (1.0 - sell_fee_rate - sell_tax_rate)
    
    net_pnl = ((net_proceeds_per_share - cost_per_share) / cost_per_share) * 100.0
    return net_pnl


def get_krx_tick_size(price: int | float) -> int:
    """KRX 및 NXT 국내 주식 가격대별 호가가격단위(Tick Size) 반환.
    - 2,000원 미만: 1원
    - 2,000원 ~ 5,000원 미만: 5원
    - 5,000원 ~ 20,000원 미만: 10원
    - 20,000원 ~ 50,000원 미만: 50원 (대한항공 등)
    - 50,000원 ~ 200,000원 미만: 100원
    - 200,000원 ~ 500,000원 미만: 500원
    - 500,000원 이상: 1,000원
    """
    p = int(abs(price))
    if p < 2000:
        return 1
    elif p < 5000:
        return 5
    elif p < 20000:
        return 10
    elif p < 50000:
        return 50
    elif p < 200000:
        return 100
    elif p < 500000:
        return 500
    else:
        return 1000


def align_to_tick_size(price: float, mode: str = "round") -> int:
    """호가가격단위(틱)에 맞게 정렬합니다.
    mode:
      - 'round': 가장 가까운 호가 단위로 반올림
      - 'floor': 해당 호가 단위 이하로 내림 (로스컷 시 안전한 하한 매도에 적합)
      - 'ceil': 해당 호가 단위 이상으로 올림 (수익컷 시 목표수익 확보에 적합)
    """
    if price <= 0:
        return 0

    tick = get_krx_tick_size(price)

    if mode == "floor":
        aligned = int(price // tick) * tick
    elif mode == "ceil":
        aligned = int(-(-price // tick)) * tick
    else:  # round
        aligned = int(round(price / tick)) * tick

    # 틱 경계선 재조정
    new_tick = get_krx_tick_size(aligned)
    if new_tick != tick:
        aligned = int(round(aligned / new_tick)) * new_tick

    return int(aligned)


def calculate_target_price_for_net_pnl(
    buy_price: float,
    target_net_pnl_pct: float,
    buy_fee_rate: float = DEFAULT_BUY_FEE_RATE,
    sell_fee_rate: float = DEFAULT_SELL_FEE_RATE,
    sell_tax_rate: float = DEFAULT_SELL_TAX_RATE
) -> int:
    """수수료 및 거래세를 차감한 뒤 '정확히 목표 순수익률(%)'이 되기 위해 도달해야 하는 목표 매도가격(원) 역산.
    반드시 KRX 호가가격단위(Tick Size)에 정렬된 유효 호가로 반환합니다.
    """
    cost_per_share = buy_price * (1.0 + buy_fee_rate)
    target_net_proceeds = cost_per_share * (1.0 + (target_net_pnl_pct / 100.0))
    raw_price = target_net_proceeds / (1.0 - sell_fee_rate - sell_tax_rate)

    # 손절(음수)이면 안전하게 floor(내림), 익절(양수)이면 ceil(올림) 적용하여 호가 단위 맞춤
    mode = "floor" if target_net_pnl_pct < 0 else "ceil"
    return align_to_tick_size(raw_price, mode=mode)
