"""Публичная раздача медиа по подписи: 200/404 без куки."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.media.public_url import sign_media_url
from app.models import (
    Attachment,
    Base,
    Contact,
    Dialog,
    Message,
    MessageDirection,
    Messenger,
)


@pytest.fixture
async def db(tmp_path):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
def client(db, tmp_path, monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    media_root = Path(settings.media_dir)
    (media_root / "in").mkdir(parents=True, exist_ok=True)
    (media_root / "in" / "f1.png").write_bytes(b"\x89PNG fake")

    async def seed():
        async with db() as s:
            s.add_all(
                [
                    Contact(id=50, messenger=Messenger.tg, external_user_id="u50"),
                    Dialog(id=11, contact_id=50, messenger=Messenger.tg, external_chat_id="c1"),
                    Message(id=1, dialog_id=11, direction=MessageDirection.inbound, text=None),
                    Attachment(
                        id=33,
                        message_id=1,
                        type="photo",
                        file_path="in/f1.png",
                        mime_type="image/png",
                        size=9,
                        file_name="foto.png",
                    ),
                ]
            )
            await s.commit()

    import asyncio

    asyncio.run(seed())
    monkeypatch.setattr("app.db.async_session", db)
    from app.web.app import create_app

    return TestClient(create_app())


def test_public_media_served_without_cookie(client):
    url = sign_media_url(
        "https://x", 33, secret="test-session-secret", ttl_sec=60
    )
    path = "/" + url.split("://x/", 1)[1]
    resp = client.get(path)
    assert resp.status_code == 200
    assert resp.content == b"\x89PNG fake"
    assert resp.headers["content-type"].startswith("image/png")
    assert "public" in resp.headers.get("cache-control", "")


def test_public_media_bad_signature_404(client):
    resp = client.get("/media/public/33/9999999999/deadbeef")
    assert resp.status_code == 404


def test_public_media_expired_404(client):
    url = sign_media_url(
        "https://x", 33, secret="test-session-secret", ttl_sec=-5
    )
    path = "/" + url.split("://x/", 1)[1]
    resp = client.get(path)
    assert resp.status_code == 404


def test_public_media_missing_file_404(client, db):
    import asyncio

    async def seed_extra():
        async with db() as s:
            s.add(
                Attachment(
                    id=34, message_id=1, type="file", file_path="in/nope.pdf",
                    mime_type="application/pdf",
                )
            )
            await s.commit()

    asyncio.run(seed_extra())
    url = sign_media_url("https://x", 34, secret="test-session-secret", ttl_sec=60)
    path = "/" + url.split("://x/", 1)[1]
    resp = client.get(path)
    assert resp.status_code == 404
