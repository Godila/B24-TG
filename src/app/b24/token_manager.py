"""TokenManager: управление OAuth-токенами Bitrix24 (загрузка + авто-refresh)."""

import logging
from datetime import UTC, datetime, timedelta

import anyio
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session
from app.models import B24Token

logger = logging.getLogger(__name__)

# Обновляем токен за 5 минут до истечения, чтобы избежать гонок.
REFRESH_MARGIN = timedelta(minutes=5)


class TokenManager:
    """Управление OAuth-токенами: загрузка из БД, авто-refresh."""

    def __init__(self, client_id: str, client_secret: str):
        self._client_id = client_id
        self._client_secret = client_secret

    async def get_token(self) -> B24Token | None:
        """Вернуть валидный токен, при необходимости обновив его."""
        token = await self._load_from_db()
        if token is None:
            return None
        if datetime.now(UTC) >= token.expires_at - REFRESH_MARGIN:
            token = await self._refresh(token)
        return token

    async def _load_from_db(self) -> B24Token | None:
        async with async_session() as session:
            result = await session.execute(select(B24Token).limit(1))
            return result.scalar_one_or_none()

    async def _refresh(self, token: B24Token) -> B24Token:
        # OAuth refresh — короткий синхронный HTTP-вызов; выполняем его в пуле
        # потоков, чтобы не блокировать event loop (ASYNC210).
        resp = await anyio.to_thread.run_sync(
            lambda: httpx.get(
                "https://oauth.bitrix24.tech/oauth/token/",
                params={
                    "grant_type": "refresh_token",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": token.refresh_token,
                },
                timeout=15,
            )
        )
        resp.raise_for_status()
        data = resp.json()
        # Персистим обновлённые значения; возвращаем свежий объект, построенный
        # из ответа OAuth, чтобы вызывающему не зависеть от состояния сессии.
        await self._save_to_db(data)
        return self._token_from_data(data)

    @staticmethod
    def _token_from_data(data: dict) -> B24Token:
        return B24Token(
            member_id=data["member_id"],
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            client_endpoint=data.get("client_endpoint", ""),
            portal=data.get("domain", ""),
            user_id=data.get("user_id", 0),
            scope=data.get("scope", ""),
            expires_at=datetime.now(UTC)
            + timedelta(seconds=data.get("expires_in", 3600)),
        )

    async def _save_to_db(self, data: dict) -> B24Token:
        async with async_session() as session:
            token = await self._upsert(session, data)
            await session.commit()
            return token

    async def _upsert(self, session: AsyncSession, data: dict) -> B24Token:
        member_id = data["member_id"]
        existing = await session.execute(
            select(B24Token).where(B24Token.member_id == member_id)
        )
        token = existing.scalar_one_or_none()
        if token is None:
            token = B24Token(member_id=member_id)
            session.add(token)
        token.access_token = data["access_token"]
        token.refresh_token = data["refresh_token"]
        token.client_endpoint = data.get("client_endpoint", token.client_endpoint)
        token.portal = data.get("domain", token.portal)
        token.user_id = data.get("user_id", token.user_id)
        token.scope = data.get("scope", token.scope)
        token.expires_at = datetime.now(UTC) + timedelta(
            seconds=data.get("expires_in", 3600)
        )
        return token
