"""Share-ссылка подключения линии: bearer-capability на QR-логин.

Админ выдаёт ссылку ``/connect/<token>``; владелец телефона открывает её
без какого-либо доступа к ЧатМост и видит QR + ссылку подтверждения +
поле 2FA. Граница доверия: в БД хранится только sha256-хэш токена
(утечка дампа не даёт ссылок), TTL ограничен, выдача новой гасит прежние,
админ может отозвать; гашение ``used_at`` — на ``authorized``.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.dialog import Messenger

if TYPE_CHECKING:
    from app.models.tg_account import TgAccount


class ConnectToken(Base, TimestampMixin):
    __tablename__ = "connect_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: sha256(urlsafe-токена) hex — сырой токен живёт только в URL.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tg_accounts.id"), nullable=False, index=True
    )
    messenger: Mapped[Messenger] = mapped_column(Enum("tg", "max", name="messenger"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Пополнено при authorized — ссылка одноразовая по смыслу.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[int] = mapped_column(Integer, ForeignKey("managers.id"), nullable=False)


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


async def issue_connect_token(
    session: AsyncSession,
    *,
    account: "TgAccount",
    created_by: int,
    ttl_sec: int,
) -> "ConnectToken":
    """Выдать токен линии; прежние живые ссылки линии-канала — отозвать
    (ровно одна активная). Сырой токен — в transient-атрибуте ``raw_token``
    (в БД только хэш); вызывающий коммитит сессию."""
    raw = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    await session.execute(
        update(ConnectToken)
        .where(
            ConnectToken.account_id == account.id,
            ConnectToken.messenger == account.messenger,
            ConnectToken.used_at.is_(None),
            ConnectToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    row = ConnectToken(
        token_hash=_hash_token(raw),
        account_id=account.id,
        messenger=account.messenger,
        expires_at=now + timedelta(seconds=ttl_sec),
        created_by=created_by,
    )
    row.raw_token = raw  # type: ignore[attr-defined]  # transient, не колонка
    session.add(row)
    return row


async def load_active_connect_token(
    session: AsyncSession, raw: str
) -> ConnectToken | None:
    """Валидный (не истёк/не отозван/не использован) токен или None.
    Lookup по UNIQUE-хэшу — перебор бессмыслен."""
    row = (
        await session.execute(
            select(ConnectToken).where(ConnectToken.token_hash == _hash_token(raw))
        )
    ).scalar_one_or_none()
    if row is None or row.used_at or row.revoked_at:
        return None
    # tz-strip с обеих сторон: SQLite возвращает naive даже для tz-колонок.
    if row.expires_at.replace(tzinfo=None) <= datetime.now(UTC).replace(tzinfo=None):
        return None
    return row
