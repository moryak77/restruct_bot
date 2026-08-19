from __future__ import annotations

import urllib.parse

import aiohttp

from core.config import config
from core.http import get_session

OPERATION_HISTORY_URL = "https://yoomoney.ru/api/operation-history"
QUICKPAY_URL = "https://yoomoney.ru/quickpay/confirm.xml"


def configured() -> bool:
    return bool(config.get("yoomoney.wallet") and config.get("yoomoney.token"))


def build_payment_link(amount: float, label: str, description: str) -> str:
    """Ссылка на форму оплаты ЮMoney (Quickpay). Оплативший может выбрать карту, СБП
    или перевод с кошелька — способ не задаём, чтобы не сужать выбор."""
    params = {
        "receiver": config.get("yoomoney.wallet"),
        "quickpay-form": "shop",
        "targets": description[:150],
        "sum": f"{amount:.2f}",
        "label": label,
    }
    return f"{QUICKPAY_URL}?{urllib.parse.urlencode(params)}"


async def fetch_recent_operations(records: int = 30) -> list[dict]:
    """Последние входящие операции кошелька. Пустой список при любой ошибке —
    вызывающий код просто попробует ещё раз на следующем цикле опроса."""
    token = config.get("yoomoney.token")
    if not token:
        return []

    headers = {"Authorization": f"Bearer {token}"}
    data = {"type": "deposition", "records": str(records)}
    try:
        async with get_session().post(
            OPERATION_HISTORY_URL, headers=headers, data=data, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            if resp.status != 200:
                return []
            payload = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError):
        return []

    return payload.get("operations", []) if isinstance(payload, dict) else []
