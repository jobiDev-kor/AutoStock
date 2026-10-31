"""텔레그램 대화형 제어 및 알림 모듈 (telegram_bot.py)

1. MTS 신규 종목 입고 시 사용자에게 알림 발송
2. 사용자가 텔레그램으로 보낸 로스컷 / 수익컷 숫자(가격 또는 %) 수신 및 설정
3. 수정(/수정), 현황(/현황), 취소(/취소) 기능 제공
"""
import re
import time
import logging
import requests
import config
import broker
import fee_calculator
import storage

logger = logging.getLogger("AutoStock.Telegram")

class TelegramNotifier:
    def __init__(self, bot_token: str = "", chat_id: str = ""):
        self.bot_token = bot_token or config.TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id or config.TELEGRAM_CHAT_ID
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
        self.last_update_id = 0

    @property
    def is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_id and "여기에" not in self.bot_token)

    def send_message(self, text: str, reply_markup: dict | None = None) -> bool:
        """텔레그램 메시지 발송."""
        if not self.is_configured:
            logger.debug("텔레그램 토큰 또는 Chat ID 가 설정되지 않아 발송을 생략합니다.")
            return False

        try:
            url = f"{self.base_url}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML"
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            res = requests.post(url, json=payload, timeout=5)
            return res.status_code == 200
        except Exception as e:
            logger.error(f"텔레그램 메시지 발송 실패: {e}")
            return False

    def notify_new_position(self, iem_cd: str, iem_nm: str, qty: int, buy_price: float):
        """MTS 신규 매수 종목 입고 알림 및 설정 요청 메시지."""
        import broker
        effective_buy = fee_calculator.get_effective_buy_price(buy_price)
        tick = fee_calculator.get_krx_tick_size(buy_price)
        is_nxt = broker.check_nxt_availability(iem_cd)
        mkt_label = "KRX + NXT (SOR 최유리 자동 라우팅)" if is_nxt else "KRX 전용 (NXT 미지원)"

        msg = (
            f"🔔 <b>[MTS 신규 종목 입고 알림]</b>\n\n"
            f"• <b>종목명</b>: {iem_nm} (<code>{iem_cd}</code>)\n"
            f"• <b>수량</b>: {qty:,}주\n"
            f"• <b>매입단가</b>: {buy_price:,.0f}원 (호가단위: {tick}원)\n"
            f"• <b>실질 평단가</b>: 약 {effective_buy:,.0f}원 (매수 수수료 포함)\n"
            f"• <b>거래가능 시장</b>: <b>{mkt_label}</b>\n\n"
            f"🎯 <b>로스컷과 상승컷을 입력해 주세요!</b>\n"
            f"입력 전까지는 시세포착 감시가 대기 상태로 유지됩니다.\n\n"
            f"<b>[입력 형식]</b>\n"
            f"1) 가격 직접 입력: <code>{iem_nm} [손절가] [익절가]</code>\n"
            f"   예시: <code>{iem_nm} 25000 27500</code>\n"
            f"2) 순수익률(%) 입력: <code>{iem_nm} [손절%] [익절%]</code>\n"
            f"   예시: <code>{iem_nm} -2% +3%</code> (제비용 자동 감안)"
        )
        self.send_message(msg)

    def poll_user_commands(self, engine) -> list[str]:
        """사용자가 텔레그램 채팅방에 보낸 메시지를 확인하고 처리합니다."""
        if not self.is_configured:
            return []

        try:
            url = f"{self.base_url}/getUpdates"
            params = {"offset": self.last_update_id + 1, "timeout": 1}
            res = requests.get(url, params=params, timeout=3)
            if res.status_code != 200:
                return []

            data = res.json()
            updates = data.get("result", [])
            handled_actions = []

            for up in updates:
                self.last_update_id = up["update_id"]

                # 1) 인라인 버튼(Inline Keyboard) 콜백 처리
                cb = up.get("callback_query")
                if cb:
                    cb_id = cb.get("id")
                    from_chat_id = str((cb.get("message") or {}).get("chat", {}).get("id") or "")
                    if self.chat_id and from_chat_id != self.chat_id:
                        continue

                    # 텔레그램 로딩 스피너 즉시 해제
                    try:
                        requests.post(f"{self.base_url}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=3)
                    except Exception:
                        pass

                    cb_data = cb.get("data", "")
                    logger.info(f"🔘 [인라인 버튼 클릭] '{cb_data}'")
                    print(f"\n🔘 [인라인 버튼 클릭] '{cb_data}'")

                    try:
                        reply = self._process_callback_query(cb_data, engine)
                        if reply:
                            self.send_message(reply)
                            handled_actions.append(f"버튼 실행: {cb_data}")
                            print(f"📤 [버튼 응답 완료] '{cb_data}'")
                    except Exception as ex:
                        logger.error(f"버튼 처리 중 오류 발생: {ex}", exc_info=True)
                        self.send_message(f"❌ 버튼 처리 중 오류: {ex}")
                    continue

                # 2) 일반 텍스트 메시지 수신 처리
                msg = up.get("message") or {}
                text = (msg.get("text") or "").strip()
                from_chat_id = str((msg.get("chat") or {}).get("id") or "")

                # 보안: 지정된 chat_id의 메시지만 수신
                if self.chat_id and from_chat_id != self.chat_id:
                    continue

                if not text:
                    continue

                logger.info(f"📨 [텔레그램 수신] '{text}'")
                print(f"\n📨 [텔레그램 수신] '{text}'")

                try:
                    # 명령어 파싱 및 처리
                    reply = self._process_command(text, engine)
                    if reply:
                        self.send_message(reply)
                        handled_actions.append(f"텔레그램 명령: {text}")
                        print(f"📤 [텔레그램 응답 완료] '{text}'")
                except Exception as ex:
                    logger.error(f"명령어 처리 중 오류 발생: {ex}", exc_info=True)
                    self.send_message(f"❌ 명령어 처리 중 오류가 발생했습니다: {ex}")

            return handled_actions

        except Exception as e:
            logger.debug(f"텔레그램 폴링 에러: {e}")
            return []

    def _parse_korean_money(self, val_str: str) -> int:
        """한글 금액 문자열을 정수 원 단위로 변환합니다.
        예: '50만' -> 500000, '50만원' -> 500000, '100만' -> 1000000, '500000' -> 500000
        """
        try:
            s = val_str.replace(",", "").replace("원", "").strip()
            if "억" in s:
                parts = s.split("억")
                b = float(parts[0]) if parts[0] else 1.0
                rem = float(parts[1].replace("만", "")) * 10000 if len(parts) > 1 and parts[1] else 0
                return int(b * 100000000 + rem)
            if "만" in s:
                n = float(s.replace("만", ""))
                return int(n * 10000)
            return int(float(s))
        except Exception:
            return 500000

    def _process_callback_query(self, cb_data: str, engine) -> str | None:
        """인라인 키보드 버튼 클릭 액션 처리."""
        if not cb_data or not engine:
            return None

        parts = cb_data.split(":")
        action = parts[0]

        if action == "BUY_NOW" and len(parts) >= 4:
            iem_cd, price_s, qty_s = parts[1], parts[2], parts[3]
            price = int(price_s)
            qty = int(qty_s)
            target = engine.ai_buy_targets.get(iem_cd)
            name = target.iem_nm if target else iem_cd

            res = broker.execute_buy_order(
                act_no=engine.act_no,
                iem_cd=iem_cd,
                qty=qty,
                price=price,
                order_type="01",
                rmt_mkt_cd="AUTO",
                dry_run=config.DRY_RUN
            )
            if res.get("success"):
                target = engine.ai_buy_targets.pop(iem_cd, None)
                storage.remove_ai_target(iem_cd)

                sell_tgt = target.sell_target_price if target and target.sell_target_price > 0 else fee_calculator.calculate_target_price_for_net_pnl(price, 3.0)
                stop_pr = target.stop_loss_price if target else 0
                storage.update_stock_target(
                    iem_cd=iem_cd,
                    iem_nm=name,
                    buy_price=price,
                    stop_loss_price=stop_pr,
                    profit_cut_price=sell_tgt,
                    loss_pct=target.loss_pct if target else 0.0,
                    profit_pct=fee_calculator.calculate_net_pnl_pct(price, sell_tgt)
                )

                loss_info = f"🔻 <b>로스컷(손절)</b>: {stop_pr:,}원" if stop_pr > 0 else "🔻 <b>로스컷(손절)</b>: 대표님 직접 설정 대기"
                return (
                    f"🛒 <b>[{name} 즉시 매수 주문 전송 완료]</b>\n\n"
                    f"• <b>종목명</b>: <b>{name}</b> (<code>{iem_cd}</code>)\n"
                    f"• <b>주문수량</b>: <b>{qty:,}주</b>\n"
                    f"• <b>주문가격</b>: <b>{price:,}원</b> (지정가)\n"
                    f"• <b>실행모드</b>: {'🟡 가상체결 (DRY_RUN)' if config.DRY_RUN else '🔴 실매매 주문'}\n\n"
                    f"🛡️ <b>[보호 및 트레일링 익절 전략]</b>\n"
                    f"• 🚀 <b>AI 매도 타점</b>: <b>{sell_tgt:,}원</b> (도달 시 <b>트레일링 익절</b>로 최고가 추적)\n"
                    f"• {loss_info}"
                )
            else:
                return f"❌ [{name} 매수 주문 실패]: {res.get('error') or res.get('reason')}"

        elif action == "AUTO_BUY" and len(parts) >= 4:
            iem_cd, price_s, qty_s = parts[1], parts[2], parts[3]
            target = engine.ai_buy_targets.get(iem_cd)
            if target:
                target.auto_buy = True
                storage.update_ai_target(
                    iem_cd=iem_cd,
                    iem_nm=target.iem_nm,
                    target_price=target.target_price,
                    target_qty=target.target_qty,
                    budget=target.budget,
                    auto_buy=True,
                    confidence=target.confidence
                )
                return (
                    f"🎯 <b>[{target.iem_nm} AI 자동 스나이퍼 활성화]</b>\n\n"
                    f"• <b>목표 매수가</b>: <b>{target.target_price:,}원 이하</b>\n"
                    f"• <b>주문 수량</b>: <b>{target.target_qty:,}주</b>\n"
                    f"• <b>상태</b>: <b>실시간 자동 매수 대기 중</b>\n\n"
                    f"장이 열리고 주가가 {target.target_price:,}원 이하로 내려오는 순간 즉시 자동 매수 주문을 집행합니다!"
                )
            return "⚠️ 등록된 AI 타점 정보를 찾을 수 없습니다."

        elif action == "CANCEL_AI" and len(parts) >= 2:
            iem_cd = parts[1]
            target = engine.ai_buy_targets.pop(iem_cd, None)
            storage.remove_ai_target(iem_cd)
            name = target.iem_nm if target else iem_cd
            return f"🗑️ <b>{name}</b>(<code>{iem_cd}</code>) AI 매수 감시를 취소했습니다."

        return None

    def _process_command(self, text: str, engine) -> str | None:
        """텔레그램 메시지 해석 및 처리 로직."""
        if not text:
            return None

        # 봇 멘션 제거 (예: /잔고@kobi32121_bot -> /잔고)
        cleaned = re.sub(r"@\w+", "", text).strip()
        parts = cleaned.split()
        if not parts:
            return None
        cmd = parts[0].lower()

        # 1) 도움말
        if cmd in ("/start", "/help", "도움말", "/도움말", "명령어", "/명령어"):
            return (
                "📖 <b>AutoStock 텔레그램 전체 명령어 가이드</b>\n\n"
                "🤖 <b>1. AI 동적 퀀트 매수 & 스나이퍼</b>\n"
                " • <code>/ai [종목] [수량/금액] [상한선] [손절] [익절]</code> : AI 타점 분석 및 자동매수 등록\n"
                "   - 예1 (자유 매수): <code>/ai 대한항공 20주 -5% 5%</code>\n"
                "   - 예2 (금액 매수): <code>/ai 삼성전자 100만 -3% 7%</code>\n"
                "   - 예3 (상한선 지정): <code>/ai SK하이닉스 10주 190000이하 -5% 5%</code>\n"
                " • <code>/ai목록</code> (또는 <code>/ai</code>, <code>/ai현황</code>) : 현재 AI 매수 감시 종목 & 목표타점 조회\n"
                " • <code>/ai분석 [종목]</code> (예: <code>/ai분석 삼성전자</code>) : 등록 없이 호가불균형/타점 단순 진단\n"
                " • <code>/ai취소 [종목]</code> (예: <code>/ai취소 이노테크</code>) : AI 매수 감시 취소\n"
                " • <code>/ai스나이퍼 [종목]</code> : 승인대기 종목을 1초 자동 스나이퍼 매수로 전환\n\n"
                "💼 <b>2. 계좌 잔고 & 보유종목 감시</b>\n"
                " • <code>/잔고</code> (또는 <code>/계좌</code>, <code>/보유</code>) : 실시간 예수금, 자산 평가액, 보유종목 목록\n"
                " • <code>/현황</code> (또는 <code>/목록</code>, <code>/status</code>) : 보유종목 감시 + AI 매수 감시 <b>통합 조회</b>\n"
                " • <code>종목명 손절% 익절%</code> (예: <code>대한항공 -3% +3%</code>) : 일반 즉시 익절/손절 감시 설정\n"
                " • <code>종목명 손절% 익절%트</code> (예: <code>대한항공 -3% +3%트</code>) : <b>🚀 트레일링 익절</b> (고점 추적)\n"
                " • <code>종목명 손절가 익절가</code> (예: <code>대한항공 25000 27500트</code>) : 가격 직접 지정\n"
                " • <code>/취소 종목명</code> (예: <code>/취소 대한항공</code>) : 보유종목 감시 취소(해제)\n\n"
                "🌐 <b>3. 실시간 시장 분석 & AI 매크로 예측</b>\n"
                " • <code>/시장</code> (또는 <code>/지수</code>, <code>/market</code>) : 코스피/코스닥 게이트, 통합 수급(KRX+NXT), 선물 베이시스, 외인 옵션, Kelly RFF 상방확률, Oxford GP 위험도 종합 리포트\n"
                " • ⏰ <b>정기 자동 브리핑</b> : 매일 <b>새벽 06:30</b> & <b>아침 07:30</b> 자동 전송 (주말/공휴일 제외)\n\n"
                "🚨 <b>4. 버스터콜(Buster Call) 비상 헤지 & 인버스 자동 매수</b>\n"
                " • <code>/버스터콜</code> : 버스터콜 상태 및 시장별 발동 현황 조회\n"
                " • <code>/버스터콜 켜기</code> (또는 <code>/버스터콜 on</code>) : 기능 활성화\n"
                " • <code>/버스터콜 끄기</code> (또는 <code>/버스터콜 off</code>) : 기능 비활성화\n"
                " • <code>/버스터콜 리셋</code> : 오늘 발동 이력 초기화 (재감시 가능)\n"
                " • <code>/버스터콜 실행</code> : 현재 시장 조건으로 즉시 헤지 집행 (수동)\n"
                " • <i>(장전 매수차단+Kelly급락 시 현물 100% 유지 & 동일금액 인버스 헤지 매수)</i>\n\n"
                "🌍 <b>5. GCFI 글로벌 자본 이동 & AI 미국 선물 예측</b>\n"
                " • <code>/글로벌</code> (또는 <code>/자본</code>, <code>/gcfi</code>) : 글로벌 자산군 순유출입, 시장 국면, 선물 방향 종합 브리핑\n"
                " • <code>/미국선물</code> (또는 <code>/선물</code>) : S&P 500(ES), 나스닥 100(NQ) AI 예측 및 Top 매크로 팩터\n"
                " • <code>/자본회전</code> (또는 <code>/회전</code>) : 어디서 빠져나간 돈이 어디로 몇 % 흡수되었는지 자본 이동 경로 추적\n\n"
                "💡 <i>MTS로 직접 매수하시면 봇이 즉시 감지하여 텔레그램으로 손절/익절 설정 버튼을 보내드립니다.</i>"
            )

        # 2) 계좌 실시간 보유종목 및 잔고 조회
        if cmd in ("/잔고", "잔고", "/계좌", "계좌", "/보유", "보유종목", "/보유종목", "/balance"):
            act_no = getattr(engine, "act_no", None) or config.DEFAULT_ACCOUNT
            summary, holdings = broker.get_account_balance(act_no)
            tot_asset = int(summary.get("tot_aet_amt") or 0)
            stock_eval = int(summary.get("tot_eal_amt") or 0)
            order_avail = int(summary.get("stk_orr_pbl_amt") or summary.get("drn_pbl_amt") or 0)
            d2_deposit = int(summary.get("nxt2_dd_dca") or order_avail)
            d0_deposit = int(summary.get("dca") or 0)

            valid_holdings = []
            for item in holdings:
                code = item.get("iem_cd")
                if not code:
                    continue
                qty = int(float(item.get("actual_qty") or item.get("rsdl_qty") or 0))
                if qty > 0:
                    valid_holdings.append((code, item, qty))

            lines = [
                "💼 <b>[실시간 계좌 자산 및 보유종목]</b>\n",
                f"• <b>계좌번호</b>: <code>{act_no}</code>",
                f"• <b>총평가자산</b>: <b>{tot_asset:,}원</b> (주식평가: {stock_eval:,}원)",
                f"• <b>주문가능금액</b>: <b>{order_avail:,}원</b> (D+2 예수금: {d2_deposit:,}원)",
                f"• <b>D일 장부예수금</b>: {d0_deposit:,}원 <i>(체결기준 미결제대기 포함)</i>\n"
            ]

            if not valid_holdings:
                lines.append("현재 계좌에 보유 중인 종목이 없습니다.")
                return "\n".join(lines)

            lines.append(f"총 <b>{len(valid_holdings)}개</b> 종목 보유 중:\n")
            for idx, (code, item, qty) in enumerate(valid_holdings, 1):
                name = item.get("iem_nm", code)
                buy_pr = float(item.get("phs_pr") or 0)
                now_pr = int(float(item.get("now_pr") or 0))
                net_pnl = fee_calculator.calculate_net_pnl_pct(buy_pr, now_pr) if buy_pr > 0 else 0.0
                pnl_str = f"{net_pnl:+.2f}%"

                # 현재 감시 상태 표시
                watch_info = engine.tracked_items.get(code) if engine else None
                if watch_info and watch_info.status == "MONITORING":
                    status_text = f"🟢 감시중 (손절 {watch_info.stop_loss_price:,} / 익절 {watch_info.profit_cut_price:,})"
                elif watch_info and watch_info.status == "WAITING_CONFIG":
                    status_text = "🟡 설정대기 (명령어 필요)"
                else:
                    status_text = "⚪ 미감시"

                is_nxt = broker.check_nxt_availability(code)
                mkt_tag = "KRX+NXT(SOR)" if is_nxt else "KRX전용"
                tick = fee_calculator.get_krx_tick_size(now_pr if now_pr > 0 else buy_pr)

                lines.append(
                    f"{idx}. <b>{name}</b> (<code>{code}</code>) [{mkt_tag}]\n"
                    f"   • 보유: {qty:,}주\n"
                    f"   • 매입단가: {buy_pr:,.0f}원 | 현재가: {now_pr:,}원 ({pnl_str})\n"
                    f"   • 호가단위: {tick:,}원 | 상태: {status_text}\n"
                )

            lines.append("👉 로스컷/상승컷을 지정하려면:\n<code>종목명 손절가 익절가</code>를 보내주세요.\n예: <code>대한항공 25000 27500</code>")
            return "\n".join(lines)

        # 2-1) 실시간 시장 파생 & 매크로 지표 조회
        if cmd in ("/시장", "시장", "/지수", "지수", "/market", "market"):
            import market_brain
            import buster_call
            mkt = market_brain.get_market_macro_snapshot(force_refresh=True)
            mgr = getattr(engine, "buster_call", None) or buster_call.buster_call_manager
            bc_st = mgr.get_status_summary()
            bc_icon = "🟢 ON" if bc_st["enabled"] else "⚪ OFF"
            bc_footer = f"\n\n🚨 <b>버스터콜 비상 헤지</b>: <b>{bc_icon}</b> (장전 매수차단+Kelly급락 시 현물유지 & 인버스 동일금액 헤지매수)"
            return mkt.briefing_text + bc_footer

        # 2-1-1) GCFI 글로벌 자본 이동 종합 브리핑 (/글로벌, /자본, /gcfi)
        if cmd in ("/글로벌", "글로벌", "/자본", "자본", "/gcfi", "gcfi", "/자금흐름", "자금흐름", "/global", "global"):
            try:
                from gcfi.telegram_reporter import format_gcfi_telegram_briefing
                return format_gcfi_telegram_briefing()
            except Exception as e:
                logger.error(f"GCFI 글로벌 브리핑 생성 오류: {e}")
                return f"⚠️ 글로벌 자본 이동 데이터 조회 중 오류가 발생했습니다: {e}"

        # 2-1-2) GCFI 미국 선물 및 AI 퀀트 방향성 진단 (/미국선물, /선물, /es, /nq)
        if cmd in ("/미국선물", "미국선물", "/선물", "선물", "/es", "/nq", "/futures", "futures"):
            try:
                from gcfi.telegram_reporter import format_futures_detail_briefing
                return format_futures_detail_briefing()
            except Exception as e:
                logger.error(f"GCFI 미국 선물 브리핑 생성 오류: {e}")
                return f"⚠️ 미국 선물 AI 진단 조회 중 오류가 발생했습니다: {e}"

        # 2-1-3) GCFI 자본 회전 경로 및 흡수 네트워크 (/자본회전, /회전)
        if cmd in ("/자본회전", "자본회전", "/회전", "회전", "/rotation", "rotation"):
            try:
                from gcfi.telegram_reporter import format_capital_rotation_briefing
                return format_capital_rotation_briefing()
            except Exception as e:
                logger.error(f"GCFI 자본 회전 브리핑 생성 오류: {e}")
                return f"⚠️ 자본 회전 네트워크 조회 중 오류가 발생했습니다: {e}"

        # 2-2) 버스터콜(Buster Call) 제어 및 상태 조회
        if cmd.startswith("/버스터콜") or cmd.startswith("버스터콜") or cmd.startswith("/bustercall") or cmd.startswith("bustercall"):
            import buster_call
            mgr = getattr(engine, "buster_call", None) or buster_call.buster_call_manager
            lower_clean = cleaned.lower()
            if any(k in lower_clean for k in ["켜기", "켜줘", " on", "start", "활성화", "/on", "1"]):
                return mgr.set_enabled(True)
            elif any(k in lower_clean for k in ["끄기", "꺼줘", " off", "stop", "비활성화", "/off", "0"]):
                return mgr.set_enabled(False)
            elif any(k in lower_clean for k in ["리셋", "초기화", "reset", "해제"]):
                target = "ALL"
                if "코스피" in lower_clean:
                    target = "KOSPI"
                elif "코스닥" in lower_clean:
                    target = "KOSDAQ"
                return mgr.reset_latch(target)
            elif any(k in lower_clean for k in ["실행", "즉시", "run", "exec"]):
                return mgr.manual_execute(engine)
            else:
                try:
                    import market_brain
                    mkt = market_brain.get_market_macro_snapshot()
                except Exception:
                    mkt = None
                return mgr.format_status_text(mkt)

        # 3) 현재 감시 현황 조회 (보유종목 감시 + AI 매수 감시 통합 출력)
        if cmd in ("/현황", "현황", "/목록", "목록", "/status"):
            has_tracked = bool(engine and engine.tracked_items)
            has_ai = bool(engine and engine.ai_buy_targets)

            if not has_tracked and not has_ai:
                return (
                    "📋 현재 감시 중인 종목이 없습니다.\n\n"
                    "• 보유종목 감시: <code>/잔고</code> 확인 후 <code>종목명 -3% +3%트</code>\n"
                    "• AI 매수 등록: <code>/ai 대한항공 20주 -5% 5%</code>"
                )

            lines = []

            # 1. 보유 종목 로스컷/익절 감시 현황
            if has_tracked:
                lines.append("📋 <b>[보유종목 로스컷/익절 감시 현황]</b>\n")
                for code, item in engine.tracked_items.items():
                    pnl_str = f"{item.pnl_pct:+.2f}%"
                    status_icon = "🟢" if item.status == "MONITORING" else "🟡"
                    trail_badge = " [🚀트레일링]" if getattr(item, 'use_trailing', False) else ""
                    lines.append(
                        f"{status_icon} <b>{item.iem_nm}</b> (<code>{code}</code>){trail_badge}\n"
                        f" • 보유: {item.hold_qty:,}주 (매도가능: {item.sellable_qty:,}주)\n"
                        f" • 매입단가: {item.buy_price:,.0f}원 | 현재가: {item.current_price:,}원 ({pnl_str})\n"
                        f" • 🔻손절가: {item.stop_loss_price:,}원 | 🔺익절가: {item.profit_cut_price:,}원\n"
                        f" • 상태: {item.status}\n"
                    )

            # 2. AI 퀀트 매수 감시 현황
            if has_ai:
                if lines:
                    lines.append("")
                is_bc_on = getattr(engine, "buster_call", None) and engine.buster_call.is_enabled
                bc_notice = " ⚠️ <i>(버스터콜 ON: 신규매수 일시휴면)</i>" if is_bc_on else ""
                lines.append(f"🧠 <b>[AI 퀀트 매수 감시 현황]</b>{bc_notice}\n")
                for code, target in engine.ai_buy_targets.items():
                    if is_bc_on:
                        mode_str = "⏸️ [휴면: 버스터콜 ON]"
                    else:
                        mode_str = "🟢 실시간 자동 스나이퍼 매수" if target.auto_buy else "🟡 승인 대기 알림 모드"
                    cap_str = f" | 상한선: {target.max_buy_price:,}원 이하" if getattr(target, 'max_buy_price', 0) > 0 else ""
                    lines.append(
                        f"• <b>{target.iem_nm}</b> (<code>{code}</code>)\n"
                        f"  - 목표타점: <b>{target.target_price:,}원</b> ({target.total_target_qty:,}주{cap_str})\n"
                        f"  - 체결: {target.accumulated_qty}주 / 잔여: {target.remaining_qty}주 (신뢰도 {target.confidence}%)\n"
                        f"  - 🔻손절: {target.loss_pct:.1f}% | 🚀트레일링: +{target.min_profit_pct:.1f}%\n"
                        f"  - 상태: {mode_str}\n"
                    )

            lines.append("👉 취소하려면: <code>/취소 종목명</code> (또는 <code>/ai취소 종목명</code>)")
            return "\n".join(lines)

        # ── AI 퀀트 매수 타점 분석 및 스나이퍼 명령 ──
        # 1) AI 동적 퀀트 매수 & 트레일링 익절 등록: /ai [종목명] [수량] [상한가] [손절] [최소수익]
        is_ai_prefix = bool(re.match(r"^/?(?:ai|ai매수|스나이퍼|동적ai|매수)", cleaned, re.IGNORECASE))
        has_dynamic_markers = any(k in cleaned for k in ["주", "이하", "상한", "스나이퍼", "동적", "로스컷", "트레일링"])
        
        # /ai 로 시작하거나, 3토큰 기존설정이 아니면서 수량/이하/상한 등의 키워드가 있는 경우
        if is_ai_prefix or (has_dynamic_markers and len(cleaned.split()) != 3):
            # 명령어 접두사 제거
            raw_text = re.sub(r"^/?(?:ai|ai매수|스나이퍼|동적ai|매수)\s*", "", cleaned, flags=re.IGNORECASE).strip()
            raw_text = raw_text.replace(",", " ").replace(":", " ")
            raw_tokens = raw_text.split()
            
            if raw_tokens:
                query = ""
                target_qty = 0
                budget = 0
                max_buy_price = 0
                loss_pct = -5.0
                min_profit_pct = 5.0
                
                idx = 0
                while idx < len(raw_tokens):
                    tok = raw_tokens[idx].strip()
                    idx += 1
                    if not tok:
                        continue
                        
                    # 한글 라벨 키워드 다음 토큰 처리
                    if tok in ("로스컷", "손절", "손절가", "손절률", "스탑로스"):
                        if idx < len(raw_tokens):
                            nxt = raw_tokens[idx].strip().replace("%", "").replace("+", "")
                            try:
                                loss_pct = -abs(float(nxt))
                                idx += 1
                            except: pass
                        continue
                        
                    if tok in ("최소수익", "최소", "수익", "익절", "상승컷", "트레일링"):
                        if idx < len(raw_tokens):
                            nxt = raw_tokens[idx].strip().replace("%", "").replace("+", "")
                            try:
                                min_profit_pct = abs(float(nxt))
                                idx += 1
                            except: pass
                        continue
                        
                    if tok in ("수량", "매수수량", "총수량", "목표수량"):
                        if idx < len(raw_tokens):
                            nxt = raw_tokens[idx].strip().replace("주", "")
                            if nxt.isdigit():
                                target_qty = int(nxt)
                                idx += 1
                        continue

                    if tok in ("상한", "상한선", "상한가", "최대가격", "최대"):
                        if idx < len(raw_tokens):
                            nxt = raw_tokens[idx].strip().replace("이하", "").replace("원", "").replace(",", "")
                            if nxt.isdigit():
                                max_buy_price = int(nxt)
                                idx += 1
                        continue

                    # 1. '이하' 포함 (예: 25000이하, 25,000원이하)
                    if "이하" in tok:
                        val_str = tok.replace("이하", "").replace("원", "").replace(",", "").strip()
                        if val_str.isdigit():
                            max_buy_price = int(val_str)
                            continue

                    # 2. '주' 포함 (예: 20주)
                    if tok.endswith("주") and tok[:-1].isdigit():
                        target_qty = int(tok[:-1])
                        continue

                    # 3. 예산 금액 (예: 50만, 100만원)
                    if "만" in tok or "억" in tok:
                        budget = self._parse_korean_money(tok)
                        continue

                    # 4. 퍼센트 (% / - / +)
                    if tok.startswith("-"):
                        val_str = tok.replace("-", "").replace("%", "").strip()
                        try:
                            loss_pct = -abs(float(val_str))
                            continue
                        except: pass

                    if tok.startswith("+"):
                        val_str = tok.replace("+", "").replace("%", "").strip()
                        try:
                            min_profit_pct = abs(float(val_str))
                            continue
                        except: pass

                    if tok.endswith("%"):
                        val_str = tok[:-1].strip()
                        try:
                            val = float(val_str)
                            if val < 0: loss_pct = val
                            else: min_profit_pct = val
                            continue
                        except: pass

                    # 5. 순수 숫자
                    clean_num = tok.replace(",", "").replace("원", "").strip()
                    if clean_num.isdigit():
                        val = int(clean_num)
                        if val >= 1000:
                            if max_buy_price == 0:
                                max_buy_price = val
                            elif budget == 0:
                                budget = val
                        elif val <= 500 and target_qty == 0:
                            target_qty = val
                        elif val <= 100:
                            min_profit_pct = float(val)
                        continue

                    # 6. 불필요 수식어 건너뛰기
                    if tok in ("총", "목표", "매수", "직접", "가격", "이상", "으로"):
                        continue

                    # 종목명/코드 설정
                    if not query:
                        query = tok

                if not query and raw_tokens:
                    query = raw_tokens[0]

                if target_qty <= 0 and budget <= 0:
                    target_qty = 20  # 기본 20주

                from quant_brain import analyze_stock_quant
                res = analyze_stock_quant(
                    query=query,
                    budget=budget,
                    target_qty=target_qty,
                    max_buy_price=max_buy_price,
                    user_stop_loss_pct=loss_pct,
                    min_profit_pct=min_profit_pct,
                    act_no=engine.act_no if engine else None
                )
                if not res:
                    return (
                        f"⚠️ '{query}' 종목을 찾을 수 없거나 시세 조회에 실패했습니다.\n"
                        f"종목명이나 6자리 코드를 확인해 주세요. (예: <code>/ai 대한항공 20주 -5% 5%</code> 또는 <code>/ai 삼성전자 10주</code>)"
                    )

                if engine:
                    from engine import AIBuyTarget
                    engine.ai_buy_targets[res.iem_cd] = AIBuyTarget(
                        iem_cd=res.iem_cd,
                        iem_nm=res.iem_nm,
                        target_price=res.recommended_entry_price,
                        target_qty=res.recommended_qty,
                        budget=res.max_budget,
                        max_buy_price=max_buy_price,
                        total_target_qty=res.recommended_qty,
                        remaining_qty=res.recommended_qty,
                        sell_target_price=res.recommended_exit_price,
                        stop_loss_price=res.user_stop_loss_price,
                        loss_pct=loss_pct,
                        min_profit_pct=min_profit_pct,
                        use_dynamic_ai=True,
                        auto_buy=True,
                        confidence=res.confidence_score
                    )
                    storage.update_ai_target(
                        iem_cd=res.iem_cd,
                        iem_nm=res.iem_nm,
                        target_price=res.recommended_entry_price,
                        target_qty=res.recommended_qty,
                        budget=res.max_budget,
                        max_buy_price=max_buy_price,
                        sell_target_price=res.recommended_exit_price,
                        stop_loss_price=res.user_stop_loss_price,
                        loss_pct=loss_pct,
                        min_profit_pct=min_profit_pct,
                        total_target_qty=res.recommended_qty,
                        remaining_qty=res.recommended_qty,
                        use_dynamic_ai=True,
                        auto_buy=True,
                        confidence=res.confidence_score
                    )

                markup = {
                    "inline_keyboard": [
                        [
                            {"text": f"⚡ 추천가 즉시매수 ({res.recommended_qty}주)", "callback_data": f"BUY_NOW:{res.iem_cd}:{res.recommended_entry_price}:{res.recommended_qty}"},
                            {"text": "🎯 1초 동적스나이퍼 자동가동", "callback_data": f"AUTO_BUY:{res.iem_cd}:{res.recommended_entry_price}:{res.recommended_qty}"}
                        ],
                        [
                            {"text": "❌ 감시 취소", "callback_data": f"CANCEL_AI:{res.iem_cd}"}
                        ]
                    ]
                }
                self.send_message(res.briefing_text, reply_markup=markup)
                return None

            markup = {
                "inline_keyboard": [
                    [
                        {"text": f"⚡ 추천가 즉시매수 ({res.recommended_qty}주)", "callback_data": f"BUY_NOW:{res.iem_cd}:{res.recommended_entry_price}:{res.recommended_qty}"},
                        {"text": "🎯 1초 동적스나이퍼 자동가동", "callback_data": f"AUTO_BUY:{res.iem_cd}:{res.recommended_entry_price}:{res.recommended_qty}"}
                    ],
                    [
                        {"text": "❌ 감시 취소", "callback_data": f"CANCEL_AI:{res.iem_cd}"}
                    ]
                ]
            }
            self.send_message(res.briefing_text, reply_markup=markup)
            return None

        # 2) AI 단순 진단/분석 리포트: /ai분석 [종목명]
        ai_diag_match = re.match(r"^/?(?:ai분석|분석|진단)\s+(\S+)", cleaned, re.IGNORECASE)
        if ai_diag_match:
            query = ai_diag_match.group(1).strip()
            from quant_brain import analyze_stock_quant
            res = analyze_stock_quant(query, 500000, act_no=engine.act_no if engine else None)
            if not res:
                return f"⚠️ '{query}' 종목을 찾을 수 없습니다."
            return res.briefing_text

        # 3) AI 매수 감시 목록: /ai목록, /ai현황, 또는 단독 /ai
        if cmd in ("/ai목록", "ai목록", "/ai현황", "ai현황", "/ai조회", "ai조회") or (cmd in ("/ai", "ai") and len(parts) == 1):
            if not engine or not engine.ai_buy_targets:
                return "📋 현재 등록된 AI 매수 감시 종목이 없습니다.\n등록 예시: <code>/ai 대한항공 50만</code>"
            is_bc_on = getattr(engine, "buster_call", None) and engine.buster_call.is_enabled
            bc_notice = " ⚠️ <i>(버스터콜 ON: 신규매수 일시휴면)</i>" if is_bc_on else ""
            lines = [f"🧠 <b>[현재 AI 매수 감시 목록]</b>{bc_notice}\n"]
            for code, target in engine.ai_buy_targets.items():
                if is_bc_on:
                    mode_str = "⏸️ [휴면: 버스터콜 ON]"
                else:
                    mode_str = "🟢 실시간 자동 스나이퍼 매수" if target.auto_buy else "🟡 승인 대기 알림 모드"
                cap_str = f" | 상한선: {target.max_buy_price:,}원 이하" if getattr(target, 'max_buy_price', 0) > 0 else ""
                lines.append(
                    f"• <b>{target.iem_nm}</b> (<code>{code}</code>)\n"
                    f"  - 목표: <b>총 {target.total_target_qty:,}주</b> (체결 {target.accumulated_qty}주 / 잔여 {target.remaining_qty}주{cap_str})\n"
                    f"  - 🔻로스컷: {target.loss_pct:.1f}% | 🚀트레일링 시작: +{target.min_profit_pct:.1f}%\n"
                    f"  - 상태: {mode_str}\n"
                )
            lines.append("👉 취소하려면: <code>/ai취소 종목명</code>\n👉 자동매수 즉시 전환: <code>/ai스나이퍼 종목명</code>")
            return "\n".join(lines)

        # 4) AI 감시 취소: /ai취소 [종목명]
        ai_cancel_match = re.match(r"^/?(?:ai취소|ai삭제)\s+(\S+)", cleaned, re.IGNORECASE)
        if ai_cancel_match:
            query = ai_cancel_match.group(1).strip()
            found_code = None
            for code, target in (engine.ai_buy_targets.items() if engine else {}):
                if query in target.iem_nm or query.upper() == code:
                    found_code = code
                    break
            if found_code:
                t = engine.ai_buy_targets.pop(found_code, None)
                storage.remove_ai_target(found_code)
                return f"🗑️ <b>{t.iem_nm}</b>(<code>{found_code}</code>) AI 매수 감시를 취소했습니다."
            return f"⚠️ '{query}' 종목을 AI 감시 목록에서 찾을 수 없습니다."

        # 5) AI 스나이퍼 즉시 전환: /ai스나이퍼 [종목명]
        ai_snipe_match = re.match(r"^/?(?:ai스나이퍼|스나이퍼전환)\s+(\S+)", cleaned, re.IGNORECASE)
        if ai_snipe_match:
            query = ai_snipe_match.group(1).strip()
            found_code = None
            for code, target in (engine.ai_buy_targets.items() if engine else {}):
                if query in target.iem_nm or query.upper() == code:
                    found_code = code
                    break
            if found_code:
                target = engine.ai_buy_targets[found_code]
                target.auto_buy = True
                storage.update_ai_target(
                    iem_cd=found_code,
                    iem_nm=target.iem_nm,
                    target_price=target.target_price,
                    target_qty=target.target_qty,
                    budget=target.budget,
                    auto_buy=True,
                    confidence=target.confidence
                )
                return f"🎯 <b>{target.iem_nm}</b>({found_code}) 목표가 {target.target_price:,}원 도달 시 <b>자동 매수</b>로 전환되었습니다!"
            return f"⚠️ '{query}' 종목이 AI 감시 목록에 없습니다. 먼저 <code>/ai {query} 50만</code>을 등록해 주세요."

        # 4) 감시 취소 (기존 보유종목 로스컷/상승컷)
        cancel_match = re.match(r"^/?(?:취소|삭제|해제)\s+(\S+)", cleaned)
        if cancel_match:
            query = cancel_match.group(1).strip()
            item = self._find_tracked_item(query, engine)
            if not item:
                return f"⚠️ '{query}' 종목을 감시 목록에서 찾을 수 없습니다."
            if engine:
                engine.tracked_items.pop(item.iem_cd, None)
            storage.remove_stock_target(item.iem_cd)
            return f"🗑️ <b>{item.iem_nm}</b>(<code>{item.iem_cd}</code>) 감시를 취소(삭제)했습니다."

        # 5) 설정 및 수정 명령: [설정/수정] [종목명/코드] [손절] [익절] (선택: '트' 또는 '트레일링')
        cmd_body = re.sub(r"^/?(?:설정|수정)\s+", "", cleaned).strip()
        body_parts = cmd_body.split()
        if len(body_parts) in (3, 4):
            name_or_code, arg1, arg2 = body_parts[0], body_parts[1], body_parts[2]
            trailing_marker = False

            # 4번째 토큰으로 '트' 또는 '트레일링'이 온 경우 (예: 대한항공 -3% +3% 트)
            if len(body_parts) == 4:
                marker = body_parts[3].lower()
                if any(k in marker for k in ["트", "trail", "추적"]):
                    trailing_marker = True

            # 3번째 토큰(arg2) 끝에 '트' 또는 '트레일링'이 붙어 있는 경우 (예: +3%트, 3%트, 28000트)
            clean_arg2_lower = arg2.lower()
            if any(k in clean_arg2_lower for k in ["트", "trail"]):
                trailing_marker = True
                arg2 = re.sub(r"[트레일링trailTRAIL]+", "", arg2).strip()

            if name_or_code.startswith("/"):
                name_or_code = name_or_code[1:]
            item = self._find_tracked_item(name_or_code, engine)
            if not item:
                return (
                    f"⚠️ '{name_or_code}' 종목을 계좌 잔고 목록에서 찾을 수 없습니다.\n"
                    f"<code>/잔고</code> 명령어로 보유 종목을 먼저 확인해 주세요."
                )

            return self._apply_targets(item, arg1, arg2, engine, use_trailing=trailing_marker)

        return (
            "❓ 인식할 수 없는 명령어입니다.\n\n"
            "• 보유 종목 조회: <code>/잔고</code>\n"
            "• 감시 현황 조회: <code>/현황</code>\n"
            "• 목표가 설정: <code>종목명 손절가 익절가</code> (예: <code>대한항공 25000 27500</code>)\n"
            "• 도움말 보기: <code>/도움말</code>"
        )

    def _find_tracked_item(self, query: str, engine):
        """종목명 또는 종목코드로 감시 아이템 검색."""
        if not engine:
            return None
        query_upper = query.upper()
        # 1. 종목코드 정확 일치
        if query_upper in engine.tracked_items:
            return engine.tracked_items[query_upper]
        # 2. 종목명 부분 일치
        for code, item in engine.tracked_items.items():
            if query in item.iem_nm or item.iem_nm in query:
                return item

        # 3. 만약 tracked_items 에 아직 없다면, 계좌 잔고에서 직접 찾아 등록
        try:
            summary, holdings = broker.get_account_balance(engine.act_no)
            for h in holdings:
                c = h.get("iem_cd", "")
                n = h.get("iem_nm", "")
                if query_upper == c or query in n or n in query:
                    qty = int(float(h.get("actual_qty") or h.get("rsdl_qty") or 0))
                    if qty > 0:
                        from engine import WatchItem
                        buy_pr = float(h.get("phs_pr") or h.get("now_pr") or 0)
                        now_pr = int(float(h.get("now_pr") or 0))
                        sellable = broker.get_sellable_quantity(engine.act_no, c)
                        watch = WatchItem(
                            iem_cd=c,
                            iem_nm=n,
                            hold_qty=qty,
                            sellable_qty=sellable if sellable > 0 else qty,
                            buy_price=buy_pr,
                            current_price=now_pr,
                            status="WAITING_CONFIG"
                        )
                        engine.tracked_items[c] = watch
                        return watch
        except Exception as e:
            logger.error(f"계좌 잔고에서 종목 검색 중 오류: {e}")
        return None

    def _apply_targets(self, item, arg1: str, arg2: str, engine, use_trailing: bool = False) -> str:
        """손절가/익절가 또는 비율을 계산하여 적용."""
        buy_price = item.buy_price if item.buy_price > 0 else (item.current_price or 10000)

        # arg2에 남아있을 수 있는 트레일링 접미사 감지 및 제거
        if any(k in arg2.lower() for k in ["트", "trail"]):
            use_trailing = True
            arg2 = re.sub(r"[트레일링trailTRAIL]+", "", arg2).strip()

        # 1) % 비율 형태인지 확인 (예: -2%, +3%, -2, 3)
        is_pct_1 = "%" in arg1
        is_pct_2 = "%" in arg2

        try:
            val1 = float(arg1.replace("%", "").replace("+", ""))
            val2 = float(arg2.replace("%", "").replace("+", ""))

            # 둘 다 100 이하의 숫자이거나 % 기호가 있으면 비율로 간주
            if is_pct_1 or is_pct_2 or (abs(val1) <= 100 and abs(val2) <= 100 and val1 < val2):
                target_type = "PERCENT"
                loss_pct = -abs(val1)
                profit_pct = abs(val2)

                stop_price = fee_calculator.calculate_target_price_for_net_pnl(buy_price, loss_pct)
                take_price = fee_calculator.calculate_target_price_for_net_pnl(buy_price, profit_pct)
            else:
                target_type = "PRICE"
                # 호가가격단위에 맞춤 (손절은 floor, 익절은 ceil)
                stop_price = fee_calculator.align_to_tick_size(min(val1, val2), mode="floor")
                take_price = fee_calculator.align_to_tick_size(max(val1, val2), mode="ceil")
                loss_pct = fee_calculator.calculate_net_pnl_pct(buy_price, stop_price)
                profit_pct = fee_calculator.calculate_net_pnl_pct(buy_price, take_price)

            # 호가단위 안내
            tick = fee_calculator.get_krx_tick_size(buy_price)

            # 아이템 및 스토리지 업데이트
            item.target_type = target_type
            item.loss_pct = loss_pct
            item.profit_pct = profit_pct
            item.stop_loss_price = stop_price
            item.profit_cut_price = take_price
            item.status = "MONITORING"   # PENDING_CONFIG 에서 실제 감시 시작으로 전환
            item.use_trailing = use_trailing
            item.trailing_active = False # 재설정 시 트레일링 활성 상태 리셋
            item.highest_price = 0

            storage.update_stock_target(
                iem_cd=item.iem_cd,
                iem_nm=item.iem_nm,
                buy_price=buy_price,
                stop_loss_price=stop_price,
                profit_cut_price=take_price,
                target_type=target_type,
                loss_pct=loss_pct,
                profit_pct=profit_pct,
                use_trailing=use_trailing
            )

            if use_trailing:
                strategy_desc = (
                    f"🚀 <b>익절 전략</b>: <b>트레일링 익절 가동 ON (+{profit_pct:+.2f}% 이상 고점 추적)</b>\n"
                    f"• {take_price:,}원 도달 시 즉시 매도하지 않고, 주가가 더 오를 때마다 최고가를 추적합니다!\n"
                    f"• 최고가 기록 후 0.8% 반락 시 최유리 고점에서 자동 익절 집행"
                )
                profit_title = f"🚀 <b>상승컷 (트레일링 시작선)</b>"
            else:
                strategy_desc = (
                    f"🔺 <b>익절 전략</b>: <b>일반 시세포착 즉시 익절 (+{profit_pct:+.2f}% 도달 시 매도)</b>\n"
                    f"💡 <i>고점을 계속 추적하고 싶으시면 뒤에 '트'를 붙여주세요! (예: <code>{item.iem_nm} {loss_pct:.0f}% +{profit_pct:.0f}%트</code>)</i>"
                )
                profit_title = f"🔺 <b>상승컷 (일반 익절가)</b>"

            return (
                f"✅ <b>[{item.iem_nm} 설정/수정 완료]</b>\n\n"
                f"• <b>종목코드</b>: <code>{item.iem_cd}</code>\n"
                f"• <b>기준 평단가</b>: {buy_price:,.0f}원 (호가단위: {tick:,}원)\n"
                f"• 🔻 <b>로스컷(손절가)</b>: <b>{stop_price:,}원</b> (순수익 {loss_pct:.2f}%)\n"
                f"• {profit_title}: <b>{take_price:,}원</b> (순수익 {profit_pct:+.2f}%)\n\n"
                f"{strategy_desc}\n\n"
                f"💡 제비용(수수료 0.015%×2 + 거래세 0.18% = 0.21%) 및 호가단위({tick}원)가 반영된 실질 순수익 기준입니다.\n"
                f"단일가 시간을 제외한 프리/메인/애프터마켓에서 <b>시세포착 감시가 활성화</b>되었습니다!"
            )

        except Exception as e:
            return f"❌ 설정 중 오류가 발생했습니다: {e}\n형식 예시: <code>대한항공 25000 27500</code>"
