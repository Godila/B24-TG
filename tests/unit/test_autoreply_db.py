"""DB-интеграция AutoReplier: решение по входящему + атомарная постановка.

Реальная in-memory SQLite (StaticPool): AutoReplier открывает собственную
сессию, как в bridge. Время сообщения — msg_timestamp (не now), расписание
по умолчанию Пн–Пт 09:00–18:00 Europe/Moscow.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.bridge.autoreply import (
    AUTO_REPLY_FIRST_ENABLED_KEY,
    AUTO_REPLY_FIRST_TEXT_KEY,
    AUTO_REPLY_OFFHOURS_ENABLED_KEY,
    AUTO_REPLY_OFFHOURS_TEXT_KEY,
    AutoReplier,
)
from app.models import (
    AppSetting,
    Base,
    Contact,
    Dialog,
    Message,
    MessageDirection,
    MessageStatus,
    Messenger,
    OutboxItem,
    TgAccount,
)

#: Четверг: 08:00 UTC = 11:00 MSK (рабочее), 19:30 UTC = 22:30 MSK (ночь).
DAY = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
NIGHT = datetime(2026, 8, 20, 19, 30, tzinfo=UTC)


@pytest.fixture
async def env():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with SessionLocal() as s:
        s.add(
            TgAccount(
                id=7,
                messenger=Messenger.tg,
                phone="+7999",
                session_path="/tmp/a7",
            )
        )
        s.add(
            Contact(id=10, messenger=Messenger.tg, external_user_id="999", name="Клиент")
        )
        s.add(
            Dialog(
                id=20,
                contact_id=10,
                messenger=Messenger.tg,
                external_chat_id="100200",
                account_id=7,
            )
        )
        await s.commit()
    yield SessionLocal
    await engine.dispose()


async def _settings(SessionLocal, *pairs: tuple[str, str]) -> None:
    async with SessionLocal() as s:
        for key, value in pairs:
            s.add(AppSetting(key=key, value=value))
        await s.commit()


async def _add_msg(
    SessionLocal,
    *,
    direction: MessageDirection,
    external_id: str,
    created_at: datetime,
    is_autoreply: bool = False,
    author_user_id: int | None = None,
) -> int:
    async with SessionLocal() as s:
        m = Message(
            dialog_id=20,
            direction=direction,
            external_message_id=external_id,
            text="текст",
            status=MessageStatus.delivered
            if direction is MessageDirection.inbound
            else MessageStatus.sent,
            created_at=created_at,
            is_autoreply=is_autoreply,
            author_user_id=author_user_id,
        )
        s.add(m)
        await s.commit()
        return m.id


async def _autoreplies(SessionLocal) -> list[Message]:
    async with SessionLocal() as s:
        return list(
            (
                await s.execute(
                    select(Message).where(
                        Message.dialog_id == 20, Message.is_autoreply.is_(True)
                    )
                )
            )
            .scalars()
            .all()
        )


async def _run(SessionLocal, message_id: int, ts: datetime | None) -> None:
    await AutoReplier(SessionLocal).on_inbound(
        message_id=message_id, account_id=7, msg_timestamp=ts
    )


@pytest.mark.asyncio
async def test_first_inbound_creates_autoreply_and_outbox_item(env):
    SessionLocal = env
    await _settings(
        SessionLocal,
        (AUTO_REPLY_FIRST_ENABLED_KEY, "on"),
        (AUTO_REPLY_FIRST_TEXT_KEY, "Здравствуйте!"),
    )
    mid = await _add_msg(
        SessionLocal,
        direction=MessageDirection.inbound,
        external_id="m1",
        created_at=DAY,
    )
    await _run(SessionLocal, mid, DAY)

    auto = await _autoreplies(SessionLocal)
    assert len(auto) == 1
    assert auto[0].text == "Здравствуйте!"
    assert auto[0].author_user_id is None
    assert auto[0].status == MessageStatus.pending
    async with SessionLocal() as s:
        item = (
            await s.execute(select(OutboxItem).where(OutboxItem.message_id == auto[0].id))
        ).scalar_one()
        assert item.is_initiation is False
        assert item.tg_account_id == 7
        assert item.external_chat_id == "100200"


@pytest.mark.asyncio
async def test_second_inbound_no_new_autoreply(env):
    SessionLocal = env
    await _settings(
        SessionLocal,
        (AUTO_REPLY_FIRST_ENABLED_KEY, "on"),
        (AUTO_REPLY_FIRST_TEXT_KEY, "Привет"),
    )
    mid1 = await _add_msg(
        SessionLocal, direction=MessageDirection.inbound, external_id="m1", created_at=DAY
    )
    await _run(SessionLocal, mid1, DAY)
    mid2 = await _add_msg(
        SessionLocal, direction=MessageDirection.inbound, external_id="m2", created_at=DAY
    )
    await _run(SessionLocal, mid2, DAY)
    assert len(await _autoreplies(SessionLocal)) == 1


@pytest.mark.asyncio
async def test_offhours_night_wins_over_first(env):
    """Ночной первый контакт: только off-hours текст, без дубля приветствия."""
    SessionLocal = env
    await _settings(
        SessionLocal,
        (AUTO_REPLY_FIRST_ENABLED_KEY, "on"),
        (AUTO_REPLY_FIRST_TEXT_KEY, "Привет"),
        (AUTO_REPLY_OFFHOURS_ENABLED_KEY, "on"),
        (AUTO_REPLY_OFFHOURS_TEXT_KEY, "Мы закрыты"),
    )
    mid = await _add_msg(
        SessionLocal, direction=MessageDirection.inbound, external_id="m1", created_at=NIGHT
    )
    await _run(SessionLocal, mid, NIGHT)
    auto = await _autoreplies(SessionLocal)
    assert len(auto) == 1 and auto[0].text == "Мы закрыты"


@pytest.mark.asyncio
async def test_night_first_inbound_when_offhours_disabled(env):
    SessionLocal = env
    await _settings(
        SessionLocal,
        (AUTO_REPLY_FIRST_ENABLED_KEY, "on"),
        (AUTO_REPLY_FIRST_TEXT_KEY, "Привет"),
    )
    mid = await _add_msg(
        SessionLocal, direction=MessageDirection.inbound, external_id="m1", created_at=NIGHT
    )
    await _run(SessionLocal, mid, NIGHT)
    auto = await _autoreplies(SessionLocal)
    assert len(auto) == 1 and auto[0].text == "Привет"


@pytest.mark.asyncio
async def test_offhours_dedup_blocks_within_24h(env):
    SessionLocal = env
    await _settings(
        SessionLocal,
        (AUTO_REPLY_OFFHOURS_ENABLED_KEY, "on"),
        (AUTO_REPLY_OFFHOURS_TEXT_KEY, "Мы закрыты"),
    )
    await _add_msg(
        SessionLocal,
        direction=MessageDirection.outbound,
        external_id="a1",
        created_at=NIGHT.replace(hour=17, minute=30),  # 2ч до NIGHT
        is_autoreply=True,
    )
    mid = await _add_msg(
        SessionLocal, direction=MessageDirection.inbound, external_id="m1", created_at=NIGHT
    )
    await _run(SessionLocal, mid, NIGHT)
    assert len(await _autoreplies(SessionLocal)) == 1  # только засеянный


@pytest.mark.asyncio
async def test_offhours_fires_after_24h(env):
    SessionLocal = env
    await _settings(
        SessionLocal,
        (AUTO_REPLY_OFFHOURS_ENABLED_KEY, "on"),
        (AUTO_REPLY_OFFHOURS_TEXT_KEY, "Мы закрыты"),
    )
    await _add_msg(
        SessionLocal,
        direction=MessageDirection.outbound,
        external_id="a1",
        created_at=datetime(2026, 8, 19, 18, 0, tzinfo=UTC),  # 25.5ч до NIGHT
        is_autoreply=True,
    )
    mid = await _add_msg(
        SessionLocal, direction=MessageDirection.inbound, external_id="m1", created_at=NIGHT
    )
    await _run(SessionLocal, mid, NIGHT)
    assert len(await _autoreplies(SessionLocal)) == 2


@pytest.mark.asyncio
async def test_offhours_skips_when_manager_answered(env):
    """Живой разговор: после предыдущего inbound отвечал менеджер — бот молчит."""
    SessionLocal = env
    await _settings(
        SessionLocal,
        (AUTO_REPLY_OFFHOURS_ENABLED_KEY, "on"),
        (AUTO_REPLY_OFFHOURS_TEXT_KEY, "Мы закрыты"),
    )
    await _add_msg(
        SessionLocal,
        direction=MessageDirection.inbound,
        external_id="m1",
        created_at=NIGHT.replace(hour=18, minute=30),
    )
    await _add_msg(
        SessionLocal,
        direction=MessageDirection.outbound,
        external_id="o1",
        created_at=NIGHT.replace(hour=18, minute=40),
        author_user_id=15,
    )
    mid = await _add_msg(
        SessionLocal, direction=MessageDirection.inbound, external_id="m2", created_at=NIGHT
    )
    await _run(SessionLocal, mid, NIGHT)
    assert await _autoreplies(SessionLocal) == []


@pytest.mark.asyncio
async def test_disabled_config_is_silent(env):
    SessionLocal = env
    mid = await _add_msg(
        SessionLocal, direction=MessageDirection.inbound, external_id="m1", created_at=DAY
    )
    await _run(SessionLocal, mid, DAY)
    assert await _autoreplies(SessionLocal) == []


@pytest.mark.asyncio
async def test_missing_message_is_noop(env):
    SessionLocal = env
    await _settings(
        SessionLocal,
        (AUTO_REPLY_FIRST_ENABLED_KEY, "on"),
        (AUTO_REPLY_FIRST_TEXT_KEY, "Привет"),
    )
    await _run(SessionLocal, message_id=999, ts=DAY)
    assert await _autoreplies(SessionLocal) == []


@pytest.mark.asyncio
async def test_enabled_with_empty_text_is_off_fail_closed(env):
    """Ключ on, но текст пуст — триггер выключен (fail-closed чтения)."""
    SessionLocal = env
    await _settings(
        SessionLocal,
        (AUTO_REPLY_FIRST_ENABLED_KEY, "on"),
        (AUTO_REPLY_FIRST_TEXT_KEY, "   "),
    )
    mid = await _add_msg(
        SessionLocal, direction=MessageDirection.inbound, external_id="m1", created_at=DAY
    )
    await _run(SessionLocal, mid, DAY)
    assert await _autoreplies(SessionLocal) == []
