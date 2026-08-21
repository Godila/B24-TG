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
    #: Токен безопасности событий (для авторизации вебхуков ONIMCONNECTOR*);
    #: B24 присылает его не всегда — опционален.
    application_token: str | None = None


class DialogOut(BaseModel):
    id: int
    contact_id: int
    contact_name: str | None = None
    messenger: str
    external_chat_id: str
    crm_deal_id: int | None = None
    title: str | None = None
    last_msg_at: datetime | None = None
    #: Ответственный или участник линии может писать (наблюдатель/supervisor — нет).
    can_write: bool = True

    @field_validator("last_msg_at", mode="after")
    @classmethod
    def _last_msg_at_utc(cls, v: datetime | None) -> datetime | None:
        return _ensure_utc(v)


class AttachmentOut(BaseModel):
    """Вложение сообщения (медиа на общем томе, раздача через API)."""

    id: int
    #: photo | file | video | voice | sticker — как рендерить в пузыре.
    type: str
    mime_type: str | None = None
    size: int | None = None
    file_name: str | None = None
    #: Авторизованный URL раздачи (/api/attachments/{id}/file).
    file_url: str


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
    #: Медиа-вложения; текст-плейсхолдер («[фото]») при наличии вложений
    #: DTO скрывает — картинка говорит сама за себя.
    attachments: list[AttachmentOut] = []

    @field_validator("created_at", mode="after")
    @classmethod
    def _created_at_utc(cls, v: datetime | None) -> datetime | None:
        return _ensure_utc(v)


class SendMessageIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4096)


class InitiateIn(BaseModel):
    """«Написать первым» из карточки CRM: резолвит bridge-воркер."""

    messenger: str = Field(..., pattern="^(tg|max)$")
    entity_type: str = Field(..., pattern="^(deal|lead|contact)$")
    entity_id: int = Field(..., gt=0)
    #: None → приоритетный аккаунт менеджера → единственный доступный.
    account_id: int | None = None
    #: Телефон (+7…) или @username (только tg); нормализация — normalize_dest.
    dest: str = Field(..., min_length=3, max_length=128)
    text: str = Field(..., min_length=1, max_length=4096)
    #: Запомнить выбранный аккаунт как приоритетный для канала.
    remember_account: bool = False


class InitiationOut(BaseModel):
    id: int
    status: str  # pending | linked | failed
    dialog_id: int | None = None
    error: str | None = None


class AccountOut(BaseModel):
    """Аккаунт для селектора «написать первым»."""

    id: int
    messenger: str
    label: str
    is_default: bool


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
    #: Тип CRM-сущности диалога ('deal'|'lead') — задаёт текст ссылки.
    crm_entity_type: str | None = None
    #: Готовая ссылка на карточку CRM B24 (None, если сущности нет).
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
    #: Диалог назначен текущему менеджеру.
    is_mine: bool = True
    #: Право записи: ответственный ИЛИ участник линии (общий номер);
    #: наблюдатель и supervisor-надзор читают, composer скрыт.
    can_write: bool = False


class InboxDialogsPageOut(BaseModel):
    """Ответ /api/inbox/dialogs: две секции списка «Чатов».

    ``unanswered`` — ВСЕ неотвеченные диалоги скоупа (кто дольше ждёт —
    выше), не пагинируются: прятать «забытый» диалог за постраничкой
    противоречит линзе DESIGN.md (возраст ожидания — сигнатура экрана).
    ``dialogs`` — отвечавшие по свежести, страница ``limit`` + keyset-курсор
    (``has_more`` — есть ли старее последней строки страницы).
    """

    unanswered: list[InboxDialogOut] = []
    dialogs: list[InboxDialogOut] = []
    has_more: bool = False


class ReadResultOut(BaseModel):
    dialog_id: int
    last_read_msg_id: int | None = None


class TemplateOut(BaseModel):
    id: int
    title: str
    body: str
    category: str | None = None
