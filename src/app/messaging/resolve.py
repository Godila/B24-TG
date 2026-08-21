"""Резолв собеседника для «написать первым»: телефон/@username → peer канала.

Контракт: один слой нормализует ввод (``normalize_dest`` — единственное
место валидации), провайдер резолвит живой сессией (``resolve_peer``).
``None`` = «не найден или скрыт настройками приватности» (терминально,
ретраи не помогут); исключение = транспорт/протокол (ретраи владельца
вызова).
"""

import re
from dataclasses import dataclass

from app.models import Messenger

#: Телефон: 7-15 цифр, опционально ведущий + (пробелы/дефисы/скобки срезаем).
_PHONE_RE = re.compile(r"^\+?\d{7,15}$")
#: Username TG: буква + 4-32 символа [A-Za-z0-9_] (не может начинаться с
#: цифры — иначе голые числа матчились бы как username).
_USERNAME_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")


@dataclass(frozen=True, slots=True)
class ParsedDest:
    kind: str  # "phone" | "username"
    value: str


@dataclass(frozen=True, slots=True)
class ResolvedPeer:
    """Резолв успешен: идентичность для Contact/Dialog + обогащение."""

    external_user_id: str
    external_chat_id: str
    name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    phone: str | None = None


def normalize_dest(messenger: Messenger, raw: str) -> ParsedDest | None:
    """Телефон или @username → ParsedDest; None = мусор.

    MAX ищет только по телефону (username-опкода нет) — для него
    ``@username`` тоже None (fail-closed, роут ответит 422).
    """
    s = (raw or "").strip()
    if not s:
        return None
    digits = re.sub(r"[ \-()]", "", s)
    if _PHONE_RE.match(digits):
        # RU-паста «8…» (11 цифр) → «+7…»; сервер MAX прощает, TG — нет.
        if len(digits) == 11 and digits[0] == "8":
            digits = "7" + digits[1:]
        return ParsedDest("phone", digits if digits.startswith("+") else f"+{digits}")
    m = _USERNAME_RE.match(s)
    if m and messenger == Messenger.tg:
        return ParsedDest("username", m.group(1).lower())
    return None
