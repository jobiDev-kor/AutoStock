# 🤖 AutoStock — NH투자증권 MTS 연동 고속 AI 자동매매 & 리스크 가드 시스템

AutoStock은 **NH투자증권 Open API**를 활용하여 장중 실시간 시세 포착, 미시구조(Microstructure) 기반 급락 방어, 코스피/코스닥 디커플링 분석, 버스터콜(Buster Call) 헤지 및 텔레그램 인터랙티브 제어를 제공하는 고성능 퀀트 트레이딩 시스템입니다.

---

## 🌟 핵심 기능

### 1. ⚡ AI 1초 동적 스나이퍼 (Dynamic AI Sniper)
- **1초 단위 실시간 호가/체결 감시**: 지정한 목표 타점에 진입하는 즉시 밀리초 단위로 분할 시장가 주문 집행.
- **다이나믹 트레일링 스탑 (Trailing Stop)**: 최고점 대비 설정 낙폭(-0.8% 등) 발생 시 수익을 보존하며 분할 익절.
- **MTS 매수 즉각 감지**: MTS로 직접 매수한 종목도 즉시 감지하여 텔레그램으로 손절/익절 대화형 인라인 버튼 제공.

### 2. 🛡️ 장중 미시구조(Microstructure) 플래시 프리즈 (Flash Freeze)
- **CME 나스닥 100 선물 & 국내 선물 실시간 연동**: 1분/3분 선물 가격 변동 속도 및 CUSUM 레짐 체인지 추적.
- **Cont-Stoikov 주문흐름 불균형(OFI) 분석**: 대형 매도벽 등장 및 매수 호가 취소 등 독성 주문흐름(Toxic Order Flow) 사전 포착.
- **초단기 5분 안전벨트**: 장중 선물 급락이나 베이시스 붕괴 발생 시 **5분간 신규 AI 주식 매수를 자동으로 일시 동결(Hold)**하여 떨어지는 칼날 회피.

### 3. 🌐 코스피 vs 코스닥 디커플링(Decoupling) 독립 게이트
- **시장 분리 방어**: 코스피 대형주가 무너져도 코스닥 중소형 수급이 양호하면 코스닥 종목은 정상 매수 지속 (독립 판정).
- **글로벌 복합 매크로 평가**: 환율(USD/KRW), 미국채 10년물 금리, WTI 유가, Bryan Kelly RFF 머신러닝 상방 확률, Oxford GP 국면전환 위험도를 종합하여 100점 만점 건강도 산출.

### 4. 🚨 버스터콜 (Buster Call) 비상 헤지
- **지수 급락 시 자산 방어**: 시장 지수가 -1.5% 하향 돌파 또는 Kelly 급락 시 기존 보유 주식을 유지하면서 **동일 금액만큼 인버스(Inverse) ETF를 자동 매수**하여 하방 리스크 헤지.
- **텔레그램 원격 제어**: `/버스터콜 켜기`, `/버스터콜 끄기`, `/버스터콜 리셋` 명령어로 장중 언제든지 설정 가능.

### 5. 📱 텔레그램(Telegram) 인터랙티브 봇
- `/시장` : 코스피/코스닥 통합 게이트, 선물 베이시스, 외인/기관 수급, AI 예측 종합 브리핑
- `/잔고` : 실시간 계좌 잔고, 예수금, 평가손익, 보유종목 즉시 확인
- 정기 브리핑: 매일 새벽 06:30 및 아침 07:30 모닝 매크로 자동 전송

---

## 🏗️ 아키텍처 구성

```
AutoStock/
├── main.py                  # 메인 이벤트 루프 및 스케줄러 진입점
├── engine.py                # 트레이딩 엔진 코어 (주문 집행, 포지션 추적, 게이팅)
├── broker.py                # NH투자증권 REST API 연동 클라이언트 (시세, 잔고, 주문)
├── market_brain.py          # 글로벌 매크로 분석, 선물 속도 감시, 플래시 프리즈
├── quant_brain.py           # Cont-Stoikov OFI, Kelly RFF 머신러닝 모델, 틱 분석
├── buster_call.py           # 버스터콜 인버스 헤지 매니저
├── telegram_bot.py          # 텔레그램 봇 인터페이스 (대화형 명령어 및 실시간 알림)
├── fee_calculator.py        # 세금/수수료 정밀 손익분기 계산기
├── market_schedule.py       # 한국 증시 개장/마감/휴장일 스케줄러
├── storage.py               # 감시 종목 및 설정 로컬 JSON 영속화
├── update_cloud.ps1         # 오라클 클라우드 원클릭 원격 배포 스크립트
├── autostock.service        # 리눅스 systemd 24시간 데몬 설정
└── requirements.txt         # 의존성 패키지 목록
```

---

## 🚀 빠른 시작 (Quick Start)

### 1. 가상환경 생성 및 의존성 설치
```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. 환경설정 (.env)
`.env.example`을 복사하여 `.env`를 생성하고 본인의 API 키 및 텔레그램 정보를 입력합니다.
```ini
# NH투자증권 API 키
NHPLUG_APP_KEY=your_app_key_here
NHPLUG_APP_SECRET=your_app_secret_here
NHPLUG_DEFAULT_ACCOUNT=your_account_no_here
NHPLUG_BASE_URL=https://api.nhplug.com:8443
NHPLUG_AUTH_URL=https://api.nhplug.com:8443

# 텔레그램 알림 봇
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here

# 트레이딩 설정
DRY_RUN=False
STOP_LOSS_PCT=2.0
TAKE_PROFIT_PCT=3.0
```

### 3. 시스템 실행
```bash
python main.py
```

---

## 🔒 보안 및 안전 수칙 (Security Notice)
- `.env` 및 계정 인증 키는 절대 GitHub 저장소에 커밋하지 않습니다. (`.gitignore` 적용 필수)
- 본 시스템은 금융 사고 방지를 위해 **시장 위험 국면 시 자동 매수 차단**, **주문 전 2중 안전 게이트 검증**을 엄격히 준수합니다.
- 실거래 전 반드시 `DRY_RUN=True` 모드로 사전 테스트를 진행하시기 바랍니다.

---

## 📄 License
This project is licensed under the MIT License.
