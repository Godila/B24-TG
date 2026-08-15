"""Протокольный уровень MAX: опкоды, фреймы, ошибки, токены.

Всё, что знает про формат проводного протокола ver=11, живёт здесь —
при дрейфе протокола правится этот модуль (рецепт майнинга бандлов
web.max.ru — в памяти проекта project-max-channel).

Формат фрейма (текстовый JSON по WS):
    {"ver": 11, "cmd": 0|1|3, "seq": <int>, "opcode": <int>, "payload": {...}}
    cmd=0 запрос/push, cmd=1 ACK-ответ (мэтчится по seq), cmd=3 ошибка.
"""

import json
from datetime import UTC, datetime

VER = 11

# Транспорт/сессия.
OP_PING = 1
OP_INIT = 6
OP_LOGIN = 19

# Чаты и сообщения.
OP_MSG_SEND = 64

# Push-опкоды (сервер → клиент).
# 128 = обновление чата: payload {chatId, unread, chat: {type, lastMessage,
#   participants, ...}} — входящее сообщение видно как chat.lastMessage
#   (поймано живьём 2026-08-15: {sender: int, id: str, time: ms, text,
#   type: "USER", attaches: [], elements: []}).
# 129 = активность пользователя в чате {chatId, userId} (typing/просмотр).
OP_CHAT_UPDATE = 128
OP_CHAT_ACTIVITY = 129

# QR-вход.
OP_QR_AUTH_REQUEST = 288
OP_QR_AUTH_POLL = 289
OP_QR_AUTH_LOGIN = 291
OP_QR_PASSWORD = 115

# Максимальный appVersion web-клиента, с которым QR ещё выдаётся (устаревший
# → cmd=3 "qr_login.disabled"). Дрейфует — обновляется из бандлов web.max.ru.
DEFAULT_APP_VERSION = "26.8.4"

DEFAULT_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_ORIGIN = "https://web.max.ru"
DEFAULT_WS_URL = "wss://ws-api.oneme.ru/websocket"


def build_user_agent(app_version: str, browser_ua: str) -> dict:
    """INIT-userAgent: повторяет текущий web-клиент (поле из его бандлов).

    ``headerUserAgent`` обязан совпадать с HTTP-заголовком User-Agent
    WS-соединения; appVersion — свежая версия web-клиента (см. константа).
    """
    return {
        "deviceType": "WEB",
        "pushDeviceType": "WEBPUSH",
        "locale": "ru",
        "deviceLocale": "ru",
        "osVersion": "Windows",
        "deviceName": "Chrome",
        "headerUserAgent": browser_ua,
        "isPwa": False,
        "appVersion": app_version,
        "screen": "1080x1920 1.0x",
        "timezone": "Europe/Moscow",
    }


def init_payload(device_id: str, user_agent: dict) -> dict:
    return {"deviceId": device_id, "userAgent": user_agent}


def login_payload(token: str) -> dict:
    """LOGIN(19): восстановление сессии на новом соединении."""
    return {
        "token": token,
        "interactive": True,
        "chatsCount": 20,
        "chatsSync": 20,
        "contactsSync": 0,
        "presenceSync": 0,
        "draftsSync": 0,
    }


def msg_send_payload(chat_id: int, text: str, cid: int) -> dict:
    """MSG_SEND(64): cid — ms-таймстамп для дедупа повторных отправок."""
    return {
        "chatId": chat_id,
        "message": {"text": text, "cid": cid, "elements": [], "attaches": []},
        "notify": True,
    }


def ms_to_datetime(ms: float | None) -> datetime | None:
    """MAX шлёт время миллисекундами; отрицательное/нулевое — не трогаем."""
    if not ms or ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


# ---------------------------------------------------------------------- #
# Ошибки cmd=3
# ---------------------------------------------------------------------- #
class MaxError(RuntimeError):
    """Базовый класс ошибок протокола MAX."""


class MaxAuthError(MaxError):
    """Токен отозван/невалиден — сессию не восстановить, нужен новый QR."""


class MaxQrDisabledError(MaxAuthError):
    """qr_login.disabled: appVersion устарела (дрейф web-клиента)."""


class MaxQrExpiredError(MaxError):
    """track.not.found: QR истёк/использован — нужен новый 288."""


class MaxThrottleError(MaxError):
    """Сервер просит подождать (лимиты не документированы)."""

    def __init__(self, payload: dict, retry_after_seconds: int = 30):
        super().__init__(f"throttled: {json.dumps(payload, ensure_ascii=False)[:200]}")
        self.retry_after_seconds = retry_after_seconds


class MaxProtocolError(MaxError):
    """Прочая ошибка протокола (payload сохранён для диагностики)."""

    def __init__(self, opcode: int, payload: dict):
        super().__init__(
            f"opcode {opcode} cmd=3: {json.dumps(payload, ensure_ascii=False)[:300]}"
        )
        self.opcode = opcode
        self.payload = payload


# Строгие маркеры auth-отказа: широкая подстрока ("auth"/"forbidden") матчила
# бы чат-уровневые коды (chat.forbidden на MSG_SEND) и убивала провайдера
# из-за ошибки одного чата.
_AUTH_MARKERS = ("unauthorized", "session_expired", "token.expired", "token.revoked")
_THROTTLE_MARKERS = ("too.many", "limit", "flood", "throttle")


def classify_error(opcode: int, payload: dict) -> MaxError:
    """Сопоставить cmd=3 payload типизированной ошибке.

    Код ошибки ищем в payload.error.code | payload.code | payload.error
    (живые ответы варьируются), затем — подстрокой по всему JSON.
    """
    code = ""
    err = payload.get("error")
    if isinstance(err, dict):
        code = str(err.get("code") or "")
    elif err is not None:
        code = str(err)
    if not code:
        code = str(payload.get("code") or "")
    hay = (code + " " + json.dumps(payload, ensure_ascii=False)).lower()

    if "track.not.found" in hay:
        return MaxQrExpiredError(code or "track.not.found")
    if "qr_login.disabled" in hay:
        return MaxQrDisabledError("qr_login.disabled")
    if any(m in hay for m in _AUTH_MARKERS):
        return MaxAuthError(code or "auth")
    if any(m in hay for m in _THROTTLE_MARKERS):
        return MaxThrottleError(payload)
    return MaxProtocolError(opcode, payload)


def extract_token(auth_payload: dict) -> str | None:
    """Токен из ответа QR_AUTH_LOGIN(291): tokenAttrs.LOGIN.token.

    Fallback — рекурсивный поиск по ключам с 'token' (приоритет ключам
    login/access, затем самый длинный): страхует от изменения схемы ответа.
    """
    attrs = auth_payload.get("tokenAttrs")
    if isinstance(attrs, dict):
        login = attrs.get("LOGIN")
        if isinstance(login, dict) and login.get("token"):
            return login["token"]

    tokens: dict[str, str] = {}

    def _walk(obj: object, path: str = "") -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                p = f"{path}.{k}" if path else str(k)
                if isinstance(v, str) and "token" in str(k).lower():
                    tokens[p] = v
                else:
                    _walk(v, p)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                _walk(v, f"{path}[{i}]")

    _walk(auth_payload)
    if not tokens:
        return None
    best: tuple[int, str] | None = None
    for path, value in tokens.items():
        lp = path.lower()
        score = 2 if ("login" in lp or "access" in lp) else 1
        if best is None or score > best[0] or (score == best[0] and len(value) > len(tokens[best[1]])):
            best = (score, path)
    return tokens[best[1]]
