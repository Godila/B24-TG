"""IM-операции Bitrix24: уведомления менеджеру + подписки на события."""

from app.b24.client import Bitrix24Client


class ImService:
    """IM-операции и event-подписки Bitrix24 поверх Bitrix24Client."""

    def __init__(self, client: Bitrix24Client):
        self._client = client

    async def send_notification(
        self,
        auth_token: str,
        user_id: int,
        message: str,
        keyboard: dict | None = None,
    ) -> int:
        """im.message.add (с KEYBOARD при наличии) — уведомления менеджерам
        (feed и админ-алерты). Возвращает id сообщения (нужен для delete)."""
        params: dict = {"DIALOG_ID": user_id, "MESSAGE": message}
        if keyboard is not None:
            params["KEYBOARD"] = keyboard
        result = await self._client.call(
            "im.message.add", auth_token=auth_token, params=params
        )
        return int(result)

    async def delete_message(self, auth_token: str, message_id: int) -> None:
        """im.message.delete — «чистый фид»: строка диалога исчезает из
        чата адресата при ответе/вытеснении. Ошибки — Bitrix24Error наружу
        (CANT_EDIT_MESSAGE интерпретирует вызывающий)."""
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
