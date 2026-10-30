"""AutoStock 설정 관리 모듈"""
import sys
import os
from pathlib import Path
from dotenv import load_dotenv

# 윈도우 콘솔 UTF-8 출력 강제 설정
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# .env 로드
env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path, override=True)

# ── 텔레그램 연동 설정
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── API 인증 및 URL
APP_KEY = os.getenv("NHPLUG_APP_KEY", "")
APP_SECRET = os.getenv("NHPLUG_APP_SECRET", "")
BASE_URL = os.getenv("NHPLUG_BASE_URL", "https://api.nhplug.com:8443")
AUTH_URL = os.getenv("NHPLUG_AUTH_URL", "https://api.nhplug.com:8443")

# ── 기본 계좌번호 (사용자 실거래 계좌)
# .env 의 NHPLUG_DEFAULT_ACCOUNT 에서 로드
DEFAULT_ACCOUNT = os.getenv("NHPLUG_DEFAULT_ACCOUNT", "")

# ── 시세포착 (로스컷 & 수익컷) 설정
# 손절률: -2.0% (매입가 대비 2% 하락 시 시장가/지정가 즉시 매도)
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "2.0"))

# 익절률: +3.0% (매입가 대비 3% 상승 시 시장가/지정가 즉시 매도)
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "3.0"))

# ── 주문 실행 모드
# DRY_RUN: True 이면 실제 주문 API를 전송하지 않고 가상 체결 로그만 기록
# 검증 후 실제 실매매 실행 시 .env 에서 DRY_RUN=False 로 설정
DRY_RUN = os.getenv("DRY_RUN", "True").strip().lower() in ("true", "1", "yes")

# 주문 구분: '05'(시장가) 권장 (시세포착 즉시 전량 매도 체결 보장) / '01'(지정가)
ORDER_PRICE_TYPE = os.getenv("ORDER_PRICE_TYPE", "05")

# 계좌 잔고 폴링 주기 (초) - NH API 실측 한도 5회/초 이하 준수 (기본 1.5초)
POLL_INTERVAL_SEC = float(os.getenv("POLL_INTERVAL_SEC", "1.5"))

# 거래시간 외 모의 테스트 허용 여부 (False: 장 시간 외에는 대기, True: 야간/주말에도 테스트 가능)
ALLOW_OFF_HOURS_TEST = os.getenv("ALLOW_OFF_HOURS_TEST", "False").strip().lower() in ("true", "1", "yes")

# 대시보드 화면 갱신 시 화면 지우기(cls) 여부 (False: 스크롤 로그 유지 권장, True: 단일 화면 갱신)
CLEAR_CONSOLE_DASHBOARD = os.getenv("CLEAR_CONSOLE_DASHBOARD", "False").strip().lower() in ("true", "1", "yes")
