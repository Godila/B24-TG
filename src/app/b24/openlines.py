"""Открытые линии Bitrix24 (imconnector): коннектор «ЧатМост».

Контракты сверены с официальной докой imconnector (08-20):
- imconnector.send.messages — входящие → чат линии; MESSAGES = блоки
  user/message/chat (build_send_message);
- ONIMCONNECTORMESSAGEADD — исходящие операторов (вебхук в
  web/routes/openline.py); im-пара нужна для send.status.delivery;
- imconnector.activate / connector.data.set — слайдер SETTING_CONNECTOR.

Все методы app-context: токен берётся из TokenManager; None-результат =
токена нет (ретраибельно, контракт тот же, что у Bitrix24Sync).
"""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from app.b24.client import Bitrix24Client
from app.b24.token_manager import TokenManager

logger = logging.getLogger(__name__)

#: Код коннектора (ID при imconnector.register; префикс-владелец «chatmost»).
CONNECTOR_ID = "chatmost"

# BB-коды B24 в тексте исходящих операторов («[b]Имя:[/b] [br]текст» —
# подпись оператора). В мессенджер уходит чистый текст: известные теги
# вырезаются, неизвестные [скобки] остаются как есть.
_BB_TAG_RE = re.compile(r"\[/?(?:b|i|u|s|br|url(?:=[^\]]*)?)\]", re.IGNORECASE)


def strip_bb(text: str) -> str:
    """Вырезать BB-коды B24 из текста оператора."""
    return _BB_TAG_RE.sub("", text)


def to_unixsec(dt: datetime) -> int:
    """datetime → unix-секунды (naive трактуем как UTC — БД может отдавать naive)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


@dataclass(frozen=True, slots=True)
class OperatorMessage:
    """Валидный элемент MESSAGES события ONIMCONNECTORMESSAGEADD."""

    im_chat_id: int
    im_message_id: int
    chat_id: str  # наш Dialog.id (эхо chat.id из send.messages)
    text: str
    user_id: int | None


@dataclass(frozen=True, slots=True)
class OperatorEvent:
    connector: str
    line: str
    messages: list[OperatorMessage]


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def parse_operator_event(payload: dict) -> OperatorEvent | None:
    """Тело вебхука → OperatorEvent; None = не событие коннектора/мусор.

    Fail-closed: битые элементы MESSAGES молча скипаются — ретрай B24 их
    не починит; пустой итог эквивалентен None.
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    connector = data.get("CONNECTOR")
    line = data.get("LINE")
    if not isinstance(connector, str) or not isinstance(line, (int, str)) or line == "":
        return None
    items = data.get("MESSAGES")
    if not isinstance(items, list):
        return None
    messages: list[OperatorMessage] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        im = item.get("im")
        chat = item.get("chat")
        message = item.get("message")
        if not isinstance(im, dict) or not isinstance(chat, dict):
            continue
        im_chat_id = _as_int(im.get("chat_id"))
        im_message_id = _as_int(im.get("message_id"))
        chat_id = chat.get("id")
        if im_chat_id is None or im_message_id is None or not isinstance(chat_id, str):
            continue
        text = ""
        user_id = None
        if isinstance(message, dict):
            text = message.get("text") or ""
            user_id = _as_int(message.get("user_id"))
        messages.append(
            OperatorMessage(
                im_chat_id=im_chat_id,
                im_message_id=im_message_id,
                chat_id=chat_id,
                text=strip_bb(text),
                user_id=user_id,
            )
        )
    if not messages:
        return None
    return OperatorEvent(connector=connector, line=str(line), messages=messages)


def build_send_message(
    *,
    message_id: str,
    dialog_id: int,
    date_unixsec: int,
    text: str | None,
    user_id: str,
    user_name: str | None,
    user_last_name: str | None = None,
    files: list[dict[str, str]] | None = None,
    chat_name: str | None = None,
) -> dict:
    """Один элемент MESSAGES для imconnector.send.messages."""
    message: dict = {"id": message_id, "date": date_unixsec, "text": text or ""}
    if files:
        message["files"] = files
    user: dict = {"id": user_id}
    if user_name:
        user["name"] = user_name
    if user_last_name:
        user["last_name"] = user_last_name
    chat: dict = {"id": str(dialog_id)}
    if chat_name:
        chat["name"] = chat_name
    return {"user": user, "message": message, "chat": chat}


def build_delivery_message(
    *,
    dialog_id: int,
    im_chat_id: int,
    im_message_id: int,
    date_unixsec: int,
) -> dict:
    """Элемент MESSAGES для imconnector.send.status.delivery.

    message.id в контракте — «внешний id сообщения»: у операторского
    сообщения единственный id, который знает B24, — im.message_id из
    события, его и отдаём (живой прогон покажет, шлёт ли B24 свой
    message.id в событии).
    """
    return {
        "im": {"chat_id": im_chat_id, "message_id": im_message_id},
        "message": {"id": str(im_message_id), "date": date_unixsec},
        "chat": {"id": str(dialog_id)},
    }


class OpenLineService:
    """imconnector.* поверх Bitrix24Client; токен — из TokenManager."""

    def __init__(self, token_mgr: TokenManager, client: Bitrix24Client):
        self._token_mgr = token_mgr
        self._client = client

    async def send_messages(self, *, line_id: str, messages: list[dict]) -> bool | None:
        """Входящее сообщение → чат линии. None = нет токена приложения."""
        token = await self._token_mgr.get_token()
        if token is None:
            return None
        await self._client.call(
            "imconnector.send.messages",
            auth_token=token.access_token,
            params={"CONNECTOR": CONNECTOR_ID, "LINE": line_id, "MESSAGES": messages},
        )
        return True

    async def send_status_delivery(
        self, *, line_id: str, messages: list[dict]
    ) -> bool | None:
        """Подтвердить доставку исходящего (B24 пометит прочитанным — его quirk)."""
        token = await self._token_mgr.get_token()
        if token is None:
            return None
        await self._client.call(
            "imconnector.send.status.delivery",
            auth_token=token.access_token,
            params={"CONNECTOR": CONNECTOR_ID, "LINE": line_id, "MESSAGES": messages},
        )
        return True

    async def activate(self, *, line_id: str, active: bool) -> bool | None:
        """Активировать/деактивировать коннектор на линии (слайдер настроек)."""
        token = await self._token_mgr.get_token()
        if token is None:
            return None
        await self._client.call(
            "imconnector.activate",
            auth_token=token.access_token,
            params={"CONNECTOR": CONNECTOR_ID, "LINE": line_id, "ACTIVE": active},
        )
        return True

    async def set_data(self, *, line_id: str, data: dict) -> bool | None:
        """Данные коннектора на линии: ID/NAME/URL_IM (слайдер настроек)."""
        token = await self._token_mgr.get_token()
        if token is None:
            return None
        await self._client.call(
            "imconnector.connector.data.set",
            auth_token=token.access_token,
            params={"CONNECTOR": CONNECTOR_ID, "LINE": line_id, "DATA": data},
        )
        return True

