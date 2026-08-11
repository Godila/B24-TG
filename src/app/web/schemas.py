"""Pydantic-схемы для API ответов/запросов Web UI."""

from datetime import datetime

from pydantic import BaseModel, Field


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
    tg_message_id: int | None = None
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
