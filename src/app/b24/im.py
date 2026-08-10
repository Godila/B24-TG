"""IM-операции Bitrix24: уведомления менеджеру + подписки на события."""

from app.b24.client import Bitrix24Client


class ImService:
    """IM-операции и event-подписки Bitrix24 поверх Bitrix24Client."""

    def __init__(self, client: Bitrix24Client):
        self._client = client

    async def notify_manager(self, auth_token: str, user_id: int, message: str) -> int:
        result = await self._client.call(
            "im.message.add",
            auth_token=auth_token,
            params={"DIALOG_ID": user_id, "MESSAGE": message},
        )
        return int(result)

    async def bind_event(self, auth_token: str, event: str, handler: str) -> bool:
        result = await self._client.call(
            "event.bind",
            auth_token=auth_token,
            params={"event": event, "handler": handler},
        )
        return bool(result)
