"""IM-операции Bitrix24: уведомления менеджеру + подписки на события."""

from app.b24.client import Bitrix24Client
from app.config import get_settings


class ImService:
    """IM-операции и event-подписки Bitrix24 поверх Bitrix24Client.

    Уведомления ходят ОТ ЧАТ-БОТА «ЧатМост», когда он зарегистрирован
    (settings.imbot_bot_id ≠ 0): сообщения живут в именованном диалоге с
    ботом, а не в «Заметках» приложения, и кнопка гашения — COMMAND.
    Без бота — фолбэк на im.message.* от приложения (LINK-кнопка).
    """

    def __init__(self, client: Bitrix24Client):
        self._client = client

    async def send_notification(
        self,
        auth_token: str,
        user_id: int,
        message: str,
        keyboard: dict | None = None,
    ) -> int:
        """Уведомление с KEYBOARD; возвращает id сообщения (нужен для delete)."""
        bot_id = get_settings().imbot_bot_id
        if bot_id:
            fields: dict = {"message": message}
            if keyboard is not None:
                fields["keyboard"] = keyboard
            result = await self._client.call(
                "imbot.v2.Chat.Message.send",
                auth_token=auth_token,
                params={"botId": bot_id, "dialogId": str(user_id), "fields": fields},
            )
            return int(result["id"])
        params: dict = {"DIALOG_ID": user_id, "MESSAGE": message}
        if keyboard is not None:
            params["KEYBOARD"] = keyboard
        result = await self._client.call(
            "im.message.add", auth_token=auth_token, params=params
        )
        return int(result)

    async def delete_message(self, auth_token: str, message_id: int) -> None:
        """«Чистый фид»: строка диалога исчезает из чата адресата при
        ответе/вытеснении. Ошибки — Bitrix24Error наружу (код трактует
        вызывающий, см. TOLERABLE_DELETE_CODES)."""
        bot_id = get_settings().imbot_bot_id
        if bot_id:
            await self._client.call(
                "imbot.v2.Chat.Message.delete",
                auth_token=auth_token,
                params={"botId": bot_id, "messageId": message_id},
            )
            return
        await self._client.call(
            "im.message.delete", auth_token=auth_token, params={"MESSAGE_ID": message_id}
        )

    async def bind_event(self, auth_token: str, event: str, handler: str) -> bool:
        result = await self._client.call(
            "event.bind",
            auth_token=auth_token,
            params={"event": event, "handler": handler},
        )
        return bool(result)
