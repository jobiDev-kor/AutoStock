"""종목별 로스컷/수익컷 설정 지속성 저장 모듈 (JSON 기반)"""
import json
import os
from pathlib import Path

TARGETS_FILE = Path(__file__).resolve().parent / "tracked_targets.json"

def load_targets() -> dict:
    """저장된 종목별 설정 로드.
    반환 구조:
    {
        "003490": {
            "iem_cd": "003490",
            "iem_nm": "대한항공",
            "buy_price": 26000,
            "stop_loss_price": 25000,
            "profit_cut_price": 27500,
            "is_active": True
        }
    }
    """
    if not TARGETS_FILE.exists():
        return {}
    try:
        with open(TARGETS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_targets(data: dict):
    """종목별 설정 파일 저장."""
    try:
        with open(TARGETS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 설정 저장 실패: {e}")


def update_stock_target(
    iem_cd: str,
    iem_nm: str,
    buy_price: float,
    stop_loss_price: int,
    profit_cut_price: int,
    target_type: str = "PRICE",
    loss_pct: float = 0.0,
    profit_pct: float = 0.0,
    use_trailing: bool = False
):
    """특정 종목의 로스컷/익절가 업데이트."""
    targets = load_targets()
    targets[iem_cd] = {
        "iem_cd": iem_cd,
        "iem_nm": iem_nm,
        "buy_price": buy_price,
        "stop_loss_price": stop_loss_price,
        "profit_cut_price": profit_cut_price,
        "target_type": target_type,
        "loss_pct": loss_pct,
        "profit_pct": profit_pct,
        "use_trailing": use_trailing,
        "is_active": True
    }
    save_targets(targets)


def remove_stock_target(iem_cd: str):
    """종목 설정 삭제."""
    targets = load_targets()
    if iem_cd in targets:
        del targets[iem_cd]
        save_targets(targets)


AI_TARGETS_FILE = Path(__file__).resolve().parent / "ai_targets.json"

def load_ai_targets() -> dict:
    """저장된 AI 매수 감시 타점 로드."""
    if not AI_TARGETS_FILE.exists():
        return {}
    try:
        with open(AI_TARGETS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_ai_targets(data: dict):
    """AI 매수 감시 타점 파일 저장."""
    try:
        with open(AI_TARGETS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ AI 설정 저장 실패: {e}")


def update_ai_target(
    iem_cd: str,
    iem_nm: str,
    target_price: int,
    target_qty: int,
    budget: int,
    max_buy_price: int = 0,
    sell_target_price: int = 0,
    stop_loss_price: int = 0,
    loss_pct: float = -5.0,
    min_profit_pct: float = 5.0,
    total_target_qty: int = 0,
    remaining_qty: int = 0,
    accumulated_qty: int = 0,
    accumulated_amt: float = 0.0,
    use_dynamic_ai: bool = True,
    auto_buy: bool = True,
    confidence: int = 0
):
    """특정 종목의 AI 매수 타점 업데이트."""
    targets = load_ai_targets()
    targets[iem_cd] = {
        "iem_cd": iem_cd,
        "iem_nm": iem_nm,
        "target_price": target_price,
        "target_qty": target_qty,
        "budget": budget,
        "max_buy_price": max_buy_price,
        "sell_target_price": sell_target_price,
        "stop_loss_price": stop_loss_price,
        "loss_pct": loss_pct,
        "min_profit_pct": min_profit_pct,
        "total_target_qty": total_target_qty or target_qty,
        "remaining_qty": remaining_qty if remaining_qty > 0 else (total_target_qty or target_qty),
        "accumulated_qty": accumulated_qty,
        "accumulated_amt": accumulated_amt,
        "use_dynamic_ai": use_dynamic_ai,
        "auto_buy": auto_buy,
        "confidence": confidence,
        "is_active": True
    }
    save_ai_targets(targets)


def remove_ai_target(iem_cd: str):
    """AI 매수 타점 삭제."""
    targets = load_ai_targets()
    if iem_cd in targets:
        del targets[iem_cd]
        save_ai_targets(targets)
