"""Регистрация чат-бота «ЧатМост» (imbot.v2) — общая для ONAPPINSTALL и scripts.

Bot.register/Command.register идемпотентны (повтор с тем же code от того же
приложения возвращает существующих), поэтому «зарегистрирован?» = «не пора ли
синхронизировать» — вызывать можно безбоязненно. bot_id хранится в
app_settings (KEY_IMBOT_BOT_ID); env IMBOT_BOT_ID — ручной фолбэк.
"""

import logging

from app.b24.client import Bitrix24Client

logger = logging.getLogger(__name__)

BOT_CODE = "chatmost"
#: Скрытая команда COMMAND-кнопки «Отвечать не нужно».
DISMISS_COMMAND = "dismiss"
#: Ключ app_settings с id зарегистрированного бота.
KEY_IMBOT_BOT_ID = "imbot_bot_id"


async def ensure_bot_registered(
    client: Bitrix24Client, auth_token: str, *, webhook_url: str
) -> int:
    """Зарегистрировать бота + команду dismiss; вернуть bot_id.

    ``webhook_url`` обязателен (eventMode=webhook) — вызывающий пропускает
    регистрацию при пустом PUBLIC_BASE_URL (LINK-фолбэк уведомлений).
    """
    bot = await client.call(
        "imbot.v2.Bot.register",
        auth_token=auth_token,
        params={
            "fields": {
                "code": BOT_CODE,
                "properties": {
                    "name": "ЧатМост",
                    "workPosition": "Уведомления о сообщениях клиентов",
                },
                "type": "bot",
                "eventMode": "webhook",
                "webhookUrl": webhook_url,
            }
        },
    )
    bot_id = int(bot["bot"]["id"])
    await client.call(
        "imbot.v2.Command.register",
        auth_token=auth_token,
        params={
            "botId": bot_id,
            "fields": {
                "command": DISMISS_COMMAND,
                "title": {"ru": "Отвечать не нужно (ЧатМост)"},
                "hidden": True,
                "common": False,
            },
        },
    )
    logger.info("imbot: бот «ЧатМост» зарегистрирован/подтвержден, id=%s", bot_id)
    return bot_id
