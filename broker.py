"""NH투자증권 Open API 연동 계층 (broker.py)

규칙 준수:
1. SDK의 nhplug.call() 및 status_of() 활용
2. rsp_cd 하드코딩 판정 금지, rsp_msg 및 결과 본문 확인
3. 전체장 필수 파라미터 aly_qut_cd="2" (명세 260911)
4. 매도 가능 수량 별도 조회 (/krstock/inquiry/v1/sellableQuantity)
5. dry_run 안전장치 지원
"""
import logging
from nhplug import call, paginate, status_of, NhplugError
import config
import fee_calculator
import market_schedule

logger = logging.getLogger("AutoStock.Broker")

def get_account_asset_status(act_no: str) -> dict:
    """계좌 자산현황 상세 조회 (/krstock/inquiry/v1/assetStatus)."""
    try:
        res = call("/krstock/inquiry/v1/assetStatus", {
            "act_no": act_no,
            "eal_aly_cd": "1",
            "aet_bse": "2",
            "qut_dit_cd": "UNT",
            "aly_qut_cd": "2"
        })
        return res.get("Output_0") or {}
    except Exception as e:
        logger.error(f"자산현황 상세 조회 실패 ({act_no}): {e}")
        return {}


def get_account_balance(act_no: str) -> tuple[dict, list[dict]]:
    """계좌 잔고 및 보유종목 리스트 전체 조회 (연속조회 paginate 적용).
    
    Returns:
        (account_summary_dict, holdings_list)
    """
    try:
        summary = {}
        all_raw_holdings = []

        # 🔴 연속조회(paginate): NH API 잔고는 10건 단위로 페이징되므로 전체 페이지를 순회합니다.
        for page in paginate("/krstock/inquiry/v1/balance", {
            "act_no": act_no,
            "bnc_bse_cd": "5",
            "ltg_aot_dit_cd": "9",
            "aet_bse": "2",
            "qut_dit_cd": "UNT",
            "aly_qut_cd": "2"
        }):
            if not summary and page.get("Output_0"):
                summary = page["Output_0"].copy()
            all_raw_holdings.extend(page.get("Output_1") or [])

        # 🔴 실시간 총자산 및 주문가능금액을 위해 assetStatus 정보 병합
        asset_info = get_account_asset_status(act_no)
        if asset_info:
            summary.update(asset_info)

        # 🔴 실시간 체결기준 실보유 종목만 엄격히 필터링
        # 당일 매도 완료되어 잔고가 0인 종목은 rsdl_qty(잔여수량)가 0이므로 제외하고,
        # 실제로 현재 보유 중인 종목(rsdl_qty > 0)만 추출합니다.
        real_holdings = []
        for h in all_raw_holdings:
            code = h.get("iem_cd")
            if not code:
                continue
            rsdl = float(h.get("rsdl_qty") or 0)
            if rsdl > 0:
                h["actual_qty"] = int(rsdl)
                real_holdings.append(h)

        return summary, real_holdings
    except NhplugError as e:
        logger.error(f"잔고 조회 실패 (계좌: {act_no}): {e.category} {e.code} {e.message}")
        return {}, []
    except Exception as e:
        logger.error(f"잔고 조회 중 예외 발생: {e}")
        return {}, []


def get_sellable_quantity(act_no: str, iem_cd: str) -> int:
    """해당 종목의 실제 매도 가능 수량 조회."""
    try:
        res = call("/krstock/inquiry/v1/sellableQuantity", {
            "act_no": act_no,
            "iem_cd": iem_cd,
            "cfd_lon_cd": "00"  # 00: 일반거래
        })
        code, msg = status_of(res)
        o0 = res.get("Output_0") or {}
        sll_qty = int(o0.get("sll_pbl_qty") or 0)
        return sll_qty
    except Exception as e:
        logger.error(f"매도가능수량 조회 실패 ({iem_cd}): {e}")
        return 0


def get_current_price(iem_cd: str, market_cd: str = "UNT") -> dict | None:
    """종목의 현재가 조회 (UNT: 통합 시세)."""
    try:
        res = call("/krstock/quote/v1/currentPrice", {
            "iem_cd": iem_cd,
            "market_cd": market_cd
        })
        code, msg = status_of(res)
        o0 = res.get("Output_0")
        if not o0:
            logger.warning(f"현재가 응답에 Output_0 없음 ({iem_cd}): {msg}")
            return None
        return o0
    except Exception as e:
        logger.error(f"현재가 조회 실패 ({iem_cd}): {e}")
        return None


def get_investor_trend(iem_cd: str, market_cd: str = "UNT") -> dict | None:
    """종목의 투자자 및 프로그램 매매 동향 조회 (UNT: KRX+NXT 통합, NXT: 대체거래소, KRX: 한국거래소)."""
    try:
        res = call("/krstock/quote/v1/currentInvestor", {
            "iem_cd": iem_cd,
            "market_cd": market_cd,
            "array_cnt": "001"
        })
        code, msg = status_of(res)
        o0 = res.get("Output_0")
        if o0 and len(o0) > 0:
            return o0[0]
        return None
    except Exception as e:
        logger.error(f"투자자 수급 조회 실패 ({iem_cd}, {market_cd}): {e}")
        return None


_nxt_support_cache: dict[str, bool] = {}

def check_nxt_availability(iem_cd: str) -> bool:
    """해당 종목이 대체거래소(NXT)에서 거래 가능한 종목인지 판별 (캐싱)."""
    if iem_cd in _nxt_support_cache:
        return _nxt_support_cache[iem_cd]

    try:
        res = call("/krstock/quote/v1/currentPrice", {
            "iem_cd": iem_cd,
            "market_cd": "NXT"
        })
        code, msg = status_of(res)
        # rsp_cd 00500 이면 NXT 거래불가 종목 (KRX 전용)
        is_supported = (code != "00500") and (res.get("Output_0") is not None)
        _nxt_support_cache[iem_cd] = is_supported
        return is_supported
    except Exception:
        _nxt_support_cache[iem_cd] = False
        return False


def resolve_best_execution_market(iem_cd: str, session_name: str) -> tuple[str, str, str]:
    """종목의 KRX/NXT 지원 여부 및 현재 세션에 따라 최유리 체결 시장(rmt_mkt_cd) 결정.
    
    Returns:
        (rmt_mkt_cd, sor_mkt_sli_yn, desc)
        - rmt_mkt_cd: 'SOR', 'KRX', 'NXT'
    """
    is_nxt = check_nxt_availability(iem_cd)

    # 1) KRX 전용 종목 (NXT 미지원)
    if not is_nxt:
        return ("KRX", "N", "KRX 전용 종목 -> KRX 단독 전송")

    # 2) KRX + NXT 복합 종목 (둘 다 매매 가능)
    if "프리마켓" in session_name:
        # 08:00 ~ 08:50 프리마켓은 KRX 미개장이므로 NXT 로 즉시 전송
        return ("NXT", "N", "NXT 프리마켓 즉시 전송")
    elif "메인마켓" in session_name or "정규장" in session_name:
        # 09:00 ~ 15:20 은 양쪽 다 열려 있으므로 SOR(스마트 최유리 집행)으로 전송
        # 증권사 SOR 엔진이 KRX와 NXT 중 바로 거래 가능한 최유리 가격 시장으로 자동 체결
        return ("SOR", "N", "SOR 최유리 집행 (KRX/NXT 중 바로 거래 가능한 시장 자동 체결)")
    elif "애프터마켓" in session_name:
        # 애프터마켓 (15:30 ~ 20:00)
        return ("SOR", "N", "애프터마켓 SOR 최유리 집행")
    else:
        return ("KRX", "N", "기타 세션 -> KRX 전송")


def execute_sell_order(
    act_no: str,
    iem_cd: str,
    qty: int,
    price: int | None = None,
    order_type: str = "05",
    rmt_mkt_cd: str = "AUTO",
    dry_run: bool = True
) -> dict:
    """현금 매도 주문 집행.
    
    Args:
        act_no: 계좌번호
        iem_cd: 종목코드 6자리
        qty: 주문수량
        price: 지정가인 경우 가격 (원)
        order_type: '05' 시장가 / '01' 지정가
        rmt_mkt_cd: 'AUTO'(최유리 자동), 'SOR', 'KRX', 'NXT'
        dry_run: True 이면 실제 전송 안 함 (시뮬레이션)
    """
    if qty <= 0:
        logger.warning(f"주문 수량이 0 이하입니다. ({iem_cd}: {qty}주) - 주문 취소")
        return {"success": False, "reason": "qty <= 0"}

    # rmt_mkt_cd 자동 결정
    if rmt_mkt_cd == "AUTO" or rmt_mkt_cd == "UNT":
        market_code, sor_split, market_desc = resolve_best_execution_market(iem_cd, "정규장")
    elif rmt_mkt_cd == "SOR":
        market_code, sor_split, market_desc = "SOR", "N", "SOR 최유리 집행"
    else:
        market_code, sor_split, market_desc = rmt_mkt_cd, "N", f"{rmt_mkt_cd} 지정 집행"

    # 시장가('05') 여부
    is_market = (order_type == "05") or (price is None)

    # 🔴 [시간외 세션 시장가 방어] 애프터마켓(15:30~20:00) / 프리마켓(08:00~08:50)은 거래소 규정상 시장가('05') 주문 불가
    # 시장가 요청 시 즉시 체결 가능한 지정가('01')로 자동 전환하여 미허용 호가 거부(10042) 방지
    session = market_schedule.get_current_market_session()
    is_off_hours = ("애프터마켓" in session.session_name) or ("프리마켓" in session.session_name)
    if is_off_hours and is_market:
        logger.warning(f"⚠️ {session.session_name}은 시장가('05') 미허용 세션입니다 -> 보통가('01' 지정가)로 자동 전환합니다.")
        is_market = False
        if not price or price <= 0:
            quote = get_current_price(iem_cd, market_cd="UNT")
            bid1 = int(float((quote or {}).get("bidp1") or 0))
            cur_p = int(float((quote or {}).get("stck_prpr") or (quote or {}).get("main_cls_prpr") or 0) )
            price = bid1 if bid1 > 0 else cur_p

    input_0 = {
        "act_no": act_no,
        "iem_cd": iem_cd,
        "orr_qty": qty,
        "nmn_pr_tp_cd": "05" if is_market else "01",
        "orr_cnd_dit_cd": "00",  # 00: 없음
        "ssl_nmn_pr_dit_cd": "00", # 00: 정상
        "rmt_mkt_cd": market_code,
        "sor_mkt_sli_yn": sor_split,
    }
    if not is_market and price is not None and price > 0:
        # KRX/NXT 호가가격단위(Tick Size) 정렬 검증
        aligned_pr = fee_calculator.align_to_tick_size(price, mode="floor")
        input_0["orr_pr"] = aligned_pr
        price = aligned_pr

    logger.info(f"🚀 [매도 주문 요청] 종목: {iem_cd}, 수량: {qty}주, 구분: {'시장가' if is_market else f'지정가({price}원)'}, 시장: {market_code} ({market_desc}), DRY_RUN={dry_run}")

    if dry_run:
        logger.info(f"🟡 [DRY_RUN 모드] 실제 주문을 발송하지 않았습니다. 페이로드: {input_0}")
        return {
            "success": True,
            "dry_run": True,
            "Input_0": input_0,
            "msg": "가상 매도 주문 성공"
        }

    try:
        res = call("/krstock/order/v1/cashSell", input_0)
        code, msg = status_of(res)
        o0 = res.get("Output_0") or {}
        order_no = o0.get("mkt_orr_no")
        is_success = bool(order_no or (o0 and ("orr_qty1" in o0 or "orr_qty" in o0)))
        if not is_success:
            logger.error(f"❌ [매도 주문 실패/거부] 종목: {iem_cd}, 코드: {code}, 메시지: {msg}")
            return {
                "success": False,
                "dry_run": False,
                "rsp_cd": code,
                "rsp_msg": msg,
                "error": f"{msg} (응답코드: {code})",
                "raw": res
            }
        logger.info(f"📩 [매도 주문 성공] 주문번호: {order_no}, 코드: {code}, 메시지: {msg}")
        return {
            "success": True,
            "dry_run": False,
            "order_no": order_no,
            "rsp_cd": code,
            "rsp_msg": msg,
            "raw": res
        }
    except NhplugError as e:
        logger.error(f"❌ 매도 주문 API 오류 ({iem_cd}): {e.category} {e.code} {e.message}")
        return {"success": False, "dry_run": False, "error": str(e)}
    except Exception as e:
        logger.error(f"❌ 매도 주문 전송 중 예외 ({iem_cd}): {e}")
        return {"success": False, "dry_run": False, "error": str(e)}


def get_buyable_quantity(act_no: str, iem_cd: str, price: int = 0, order_type: str = "01") -> dict:
    """해당 종목의 주문가능 수량 및 금액 조회 (/krstock/inquiry/v1/buyableQuantity)."""
    try:
        payload = {
            "ost_dit_cd": "1",  # 1: 현금
            "act_no": act_no,
            "iem_cd": iem_cd,
            "nmn_pr_tp_cd": order_type
        }
        if price > 0:
            payload["orr_pr"] = price
        res = call("/krstock/inquiry/v1/buyableQuantity", payload)
        code, msg = status_of(res)
        o0 = res.get("Output_0") or {}
        return {
            "max_pbl_qty": int(o0.get("max_pbl_qty") or 0),
            "max_pbl_amt": int(o0.get("max_pbl_amt") or 0),
            "csh_orr_pbl_amt": int(o0.get("csh_orr_pbl_amt") or 0),
            "dca": int(o0.get("dca") or 0),
            "nxt2_dd_dca": int(o0.get("nxt2_dd_dca") or 0),
            "raw": o0
        }
    except Exception as e:
        logger.error(f"주문가능수량 조회 실패 ({iem_cd}): {e}")
        return {"max_pbl_qty": 0, "max_pbl_amt": 0, "csh_orr_pbl_amt": 0, "dca": 0, "nxt2_dd_dca": 0}


def execute_buy_order(
    act_no: str,
    iem_cd: str,
    qty: int,
    price: int | None = None,
    order_type: str = "01",
    rmt_mkt_cd: str = "AUTO",
    dry_run: bool = True
) -> dict:
    """현금 매수 주문 집행 (/krstock/order/v1/cashBuy).
    
    Args:
        act_no: 계좌번호
        iem_cd: 종목코드 6자리
        qty: 주문수량
        price: 지정가인 경우 가격 (원)
        order_type: '01' 지정가 / '05' 시장가
        rmt_mkt_cd: 'AUTO'(최유리 자동), 'SOR', 'KRX', 'NXT'
        dry_run: True 이면 실제 전송 안 함 (시뮬레이션)
    """
    if qty <= 0:
        logger.warning(f"매수 수량이 0 이하입니다. ({iem_cd}: {qty}주) - 주문 취소")
        return {"success": False, "reason": "qty <= 0"}

    # rmt_mkt_cd 자동 결정
    if rmt_mkt_cd == "AUTO" or rmt_mkt_cd == "UNT":
        market_code, sor_split, market_desc = resolve_best_execution_market(iem_cd, "정규장")
    elif rmt_mkt_cd == "SOR":
        market_code, sor_split, market_desc = "SOR", "N", "SOR 최유리 집행"
    else:
        market_code, sor_split, market_desc = rmt_mkt_cd, "N", f"{rmt_mkt_cd} 지정 집행"

    is_market = (order_type == "05") or (price is None)

    # 🔴 [시간외 세션 시장가 방어] 애프터마켓(15:30~20:00) / 프리마켓(08:00~08:50)은 거래소 규정상 시장가('05') 주문 불가
    session = market_schedule.get_current_market_session()
    is_off_hours = ("애프터마켓" in session.session_name) or ("프리마켓" in session.session_name)
    if is_off_hours and is_market:
        logger.warning(f"⚠️ {session.session_name}은 시장가('05') 미허용 세션입니다 -> 보통가('01' 지정가)로 자동 전환합니다.")
        is_market = False
        if not price or price <= 0:
            quote = get_current_price(iem_cd, market_cd="UNT")
            ask1 = int(float((quote or {}).get("askp1") or 0))
            cur_p = int(float((quote or {}).get("stck_prpr") or (quote or {}).get("main_cls_prpr") or 0))
            price = ask1 if ask1 > 0 else cur_p

    input_0 = {
        "act_no": act_no,
        "iem_cd": iem_cd,
        "orr_qty": qty,
        "nmn_pr_tp_cd": "05" if is_market else "01",
        "orr_cnd_dit_cd": "00",  # 00: 없음
        "ssl_nmn_pr_dit_cd": "00", # 00: 정상
        "rmt_mkt_cd": market_code,
        "sor_mkt_sli_yn": sor_split,
    }
    if not is_market and price is not None and price > 0:
        aligned_pr = fee_calculator.align_to_tick_size(price, mode="floor")
        input_0["orr_pr"] = aligned_pr
        price = aligned_pr

    logger.info(f"🛒 [매수 주문 요청] 종목: {iem_cd}, 수량: {qty}주, 구분: {'시장가' if is_market else f'지정가({price}원)'}, 시장: {market_code} ({market_desc}), DRY_RUN={dry_run}")

    if dry_run:
        logger.info(f"🟡 [DRY_RUN 모드] 실제 매수 주문을 발송하지 않았습니다. 페이로드: {input_0}")
        return {
            "success": True,
            "dry_run": True,
            "Input_0": input_0,
            "msg": "가상 매수 주문 성공"
        }

    try:
        res = call("/krstock/order/v1/cashBuy", input_0)
        code, msg = status_of(res)
        o0 = res.get("Output_0") or {}
        order_no = o0.get("mkt_orr_no")
        is_success = bool(order_no or (o0 and ("orr_qty1" in o0 or "orr_qty" in o0)))
        if not is_success:
            logger.error(f"❌ [매수 주문 실패/거부] 종목: {iem_cd}, 코드: {code}, 메시지: {msg}")
            return {
                "success": False,
                "dry_run": False,
                "rsp_cd": code,
                "rsp_msg": msg,
                "error": f"{msg} (응답코드: {code})",
                "raw": res
            }
        logger.info(f"📩 [매수 주문 성공] 주문번호: {order_no}, 코드: {code}, 메시지: {msg}")
        return {
            "success": True,
            "dry_run": False,
            "order_no": order_no,
            "rsp_cd": code,
            "rsp_msg": msg,
            "raw": res
        }
    except NhplugError as e:
        logger.error(f"❌ 매수 주문 API 오류 ({iem_cd}): {e.category} {e.code} {e.message}")
        return {"success": False, "dry_run": False, "error": str(e)}
    except Exception as e:
        logger.error(f"❌ 매수 주문 전송 중 예외 ({iem_cd}): {e}")
        return {"success": False, "dry_run": False, "error": str(e)}
