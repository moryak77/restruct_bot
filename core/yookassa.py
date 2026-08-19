from __future__ import annotations

import aiohttp

from core.config import config
from core.http import get_session

API_BASE = "https://api.yookassa.ru/v3"


def configured() -> bool:
    return bool(config.get("yookassa.shop_id") and config.get("yookassa.secret_key"))


def _auth() -> aiohttp.BasicAuth:
    return aiohttp.BasicAuth(str(config.get("yookassa.shop_id")), str(config.get("yookassa.secret_key")))


async def create_payment(amount: float, description: str, order_id: int, idempotence_key: str) -> dict | None:
    """Создаёт платёж на стороне ЮKassa и возвращает объект с полями id/status/confirmation.
    confirmation.confirmation_url — ссылка, на которую отправляем покупателя."""
    payload = {
        "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": config.get("yookassa.return_url", "https://discord.com")},
        "capture": True,
        "description": description[:128],
        "metadata": {"order_id": str(order_id)},
    }
    headers = {"Idempotence-Key": idempotence_key}
    try:
        async with get_session().post(
            f"{API_BASE}/payments", json=payload, headers=headers, auth=_auth(),
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status not in (200, 201):
                return None
            return await resp.json()
    except (aiohttp.ClientError, TimeoutError):
        return None


async def get_payment(payment_id: str) -> dict | None:
    """Запрашивает актуальный статус платежа напрямую у ЮKassa — единственный источник
    истины. Данные из самого вебхука никогда не используются напрямую, только как повод
    сходить и проверить, чтобы исключить подделку уведомления."""
    try:
        async with get_session().get(
            f"{API_BASE}/payments/{payment_id}", auth=_auth(), timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                return None
            return await resp.json()
    except (aiohttp.ClientError, TimeoutError):
        return None
