"""Pydantic-схемы для API ответов/запросов Web UI."""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _ensure_utc(v: datetime | None) -> datetime | None:
    """Naive-datetime (SQLite-дев) → aware UTC.

    Без зоны браузер трактует ISO-строку как ЛОКАЛЬНОЕ время — сдвиг на
    таймзону клиента и враньё «сегодня/вчера» у границы суток. Прод-значения
    (PostgreSQL timestamptz) уже aware и проходят насквозь. Нормализация
    здесь держит дев и прод одинаковыми (DESIGN.md, UX-05).
    """
    if v is not None and v.tzinfo is None:
        return v.replace(tzinfo=UTC)
    return v


class OnAppInstallAuth(BaseModel):
    """Поле auth из ONAPPINSTALL payload (строгая форма — лишнее/битое отвергаем).

    Ключи сверены с ``TokenManager.save_install_data``: access/refresh/member_id
    обязательны там жёстко (KeyError), остальные приходят в реальном payload B24.
    ``user_id``/``expires_in`` B24 может присылать строками — pydantic v2 lax-mode
    приводит их к int.
    """

    model_config = ConfigDict(extra="forbid")

    access_token: str
    refresh_token: str
    member_id: str
    client_endpoint: str
    domain: str
    user_id: int
    expires_in: int
    scope: str


class DialogOut(BaseModel):
    id: int
    contact_id: int
    contact_name: str | None = None
    messenger: str
    external_chat_id: str
    crm_deal_id: int | None = None
    title: str | None = None
    last_msg_at: datetime | None = None

    @field_validator("last_msg_at", mode="after")
    @classmethod
    def _last_msg_at_utc(cls, v: datetime | None) -> datetime | None:
        return _ensure_utc(v)


class MessageOut(BaseModel):
    id: int
    dialog_id: int
    direction: str
    text: str | None = None
    status: str
    external_message_id: str | None = None
    author_user_id: int | None = None
    timeline_comment_id: int | None = None
    created_at: datetime | None = None

    @field_validator("created_at", mode="after")
    @classmethod
    def _created_at_utc(cls, v: datetime | None) -> datetime | None:
        return _ensure_utc(v)


class SendMessageIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4096)


class InboxDialogOut(BaseModel):
    """Строка списка «Чатов» (общий мессенджер): диалог + агрегаты на лету.

    Отдельная схема (а не расширение DialogOut): контракт виджета сделки
    не меняется, а inbox-списку нужны счётчики/превью/ответственный.
    """

    id: int
    contact_id: int
    contact_name: str | None = None
    messenger: str
    title: str | None = None
    crm_deal_id: int | None = None
    #: Готовая ссылка на карточку сделки B24 (None, если сделки нет).
    deal_url: str | None = None
    last_msg_at: datetime | None = None

    @field_validator("last_msg_at", mode="after")
    @classmethod
    def _last_msg_at_utc(cls, v: datetime | None) -> datetime | None:
        return _ensure_utc(v)

    #: Превью последнего сообщения (None у диалога без сообщений).
    last_message_direction: str | None = None
    last_message_text: str | None = None
    #: Входящие после последнего исходящего (или все, если исходящих нет).
    unanswered_count: int = 0
    #: Входящие с id > last_read_msg_id владельца.
    unread_count: int = 0
    #: Ответственный менеджер (имя — только в supervisor-виде списка).
    assigned_manager_id: int | None = None
    assigned_manager_name: str | None = None
    #: Диалог назначен текущему менеджеру: только владелец пишет и гасит
    #: непрочитанные; supervisor читает чужие, composer скрыт.
    is_mine: bool = True


class ReadResultOut(BaseModel):
    dialog_id: int
    last_read_msg_id: int | None = None


class TemplateOut(BaseModel):
    id: int
    title: str
    body: str
    category: str | None = None
