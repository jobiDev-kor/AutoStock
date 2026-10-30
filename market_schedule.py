"""KRX 및 NXT(대체거래소) 거래시간 세션 판별 모듈

단일가 매매 시간을 철저히 제외하고 실시간 접속매매가 가능한 세션에서만 주문을 집행합니다.
- 프리마켓: 08:00 ~ 08:50 (NXT 접속매매)
- 단일가 제외: 08:50 ~ 09:00 (KRX/NXT 시가 단일가)
- 메인마켓(정규장): 09:00 ~ 15:20 (KRX/NXT 실시간 접속매매)
- 단일가 제외: 15:20 ~ 15:30 (KRX/NXT 종가 단일가)
- 애프터마켓: 15:30 ~ 20:00 (실시간 접속매매 / 19:50~20:00 NXT 단일가 주의)
"""
from datetime import datetime, time, date
import config

class MarketSession:
    def __init__(self, is_tradeable: bool, session_name: str, rmt_mkt_cd: str, note: str = ""):
        self.is_tradeable = is_tradeable      # 실시간 즉시 주문 가능 여부
        self.session_name = session_name      # 현재 세션 이름
        self.rmt_mkt_cd = rmt_mkt_cd          # 추천 거래소 코드 ('KRX', 'NXT', 'SOR')
        self.note = note

    def __str__(self):
        status = "[가능]" if self.is_tradeable else "[대기]"
        return f"{status} {self.session_name} (거래소: {self.rmt_mkt_cd}) - {self.note}"


def is_market_holiday(target_date: date | None = None) -> bool:
    """주말 및 한국 공휴일/증시 휴장일 여부를 판별합니다."""
    if target_date is None:
        target_date = datetime.now().date()

    # 1. 주말 (토요일: 5, 일요일: 6)
    if target_date.weekday() >= 5:
        return True

    # 2. 12월 31일 연말 증시 휴장일
    if target_date.month == 12 and target_date.day == 31:
        return True

    # 3. 법정 공휴일 (신정, 설날, 삼일절, 어린이날, 석가탄신일, 현충일, 광복절, 추석, 개천절, 한글날, 성탄절, 근로자의 날 5/1 및 대체공휴일)
    try:
        import holidays
        kr_holidays = holidays.KR(years=[target_date.year])
        if target_date in kr_holidays:
            return True
    except Exception:
        pass

    return False


def get_current_market_session(now: datetime | None = None) -> MarketSession:
    """현재 시각을 기준으로 실시간 접속매매 가능 세션인지 판별합니다."""
    if now is None:
        now = datetime.now()

    # 주말 및 공휴일 확인
    if is_market_holiday(now.date()):
        if config.ALLOW_OFF_HOURS_TEST:
            return MarketSession(True, "휴일 테스트 모드", "KRX", "ALLOW_OFF_HOURS_TEST 활성화")
        return MarketSession(False, "증시 휴장일", "NONE", "주말 또는 법정 공휴일은 증시 휴장일입니다.")

    t = now.time()

    # 1) 프리마켓 (08:00 ~ 08:50) - NXT 접속매매
    if time(8, 0, 0) <= t < time(8, 50, 0):
        return MarketSession(True, "NXT 프리마켓", "NXT", "08:00 ~ 08:50 실시간 접속매매")

    # 2) 시가 단일가 시간대 (08:50 ~ 09:00) - 단일가 매매 제외
    elif time(8, 50, 0) <= t < time(9, 0, 0):
        return MarketSession(False, "장시작 단일가 구간", "KRX", "08:50 ~ 09:00 시가 결정 단일가로 주문 보류")

    # 3) 메인마켓 정규장 (09:00 ~ 15:20) - KRX 및 NXT 실시간 접속매매
    elif time(9, 0, 0) <= t < time(15, 20, 0):
        return MarketSession(True, "메인마켓 (정규장)", "KRX", "09:00 ~ 15:20 KRX/NXT 실시간 접속매매")

    # 4) 종가 단일가 시간대 (15:20 ~ 15:30) - 단일가 매매 제외
    elif time(15, 20, 0) <= t < time(15, 30, 0):
        return MarketSession(False, "장마감 단일가 구간", "KRX", "15:20 ~ 15:30 종가 결정 단일가로 주문 보류")

    # 5) 애프터마켓 (15:30 ~ 19:50) - 실시간 접속매매
    elif time(15, 30, 0) <= t < time(19, 50, 0):
        return MarketSession(True, "애프터마켓", "NXT", "15:30 ~ 19:50 KRX/NXT 애프터마켓 접속매매")

    # 6) 애프터마켓 종가 단일가 구간 (19:50 ~ 20:00)
    elif time(19, 50, 0) <= t < time(20, 0, 0):
        # NXT는 19:50~20:00 단일가. KRX는 애프터마켓 접속매매 가능하나 안전을 위해 KRX 지정
        return MarketSession(True, "애프터마켓 마감구간(KRX)", "KRX", "19:50 ~ 20:00 KRX 애프터마켓")

    # 7) 장 마감 시간 (20:00 ~ 익일 08:00)
    else:
        if config.ALLOW_OFF_HOURS_TEST:
            return MarketSession(True, "야간 테스트 모드", "KRX", "ALLOW_OFF_HOURS_TEST 활성화")
        return MarketSession(False, "장 마감", "NONE", "20:00 ~ 08:00 증시 운영시간 외")
