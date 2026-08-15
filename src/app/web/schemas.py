"""Pydantic-схемы для API ответов/запросов Web UI."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


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


class SendMessageIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4096)


class TemplateOut(BaseModel):
    id: int
    title: str
    body: str
    category: str | None = None
