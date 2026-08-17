"""Интеграционные тесты медиа: POST /dialogs/{id}/media + раздача вложений.

Окружение: in-memory SQLite (паттерн test_dialogs_api) + MEDIA_DIR на
tmp_path (conftest сбрасывает кэши настроек/storage на каждый тест).
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


@pytest.fixture
async def media_app(tmp_path, monkeypatch):
    """Приложение с медиа-томом на tmp_path: загрузки пишутся в out/,
    раздача читает оттуда же."""
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        from app.models import Base

        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    from app.models import Contact, Dialog, Manager, Messenger, TgAccount

    async with SessionLocal() as s:
        s.add(Manager(id=1, name="Иван", b24_user_id=15, is_active=True))
        s.add(
            TgAccount(
                id=7,
                messenger=Messenger.tg,
                phone="+79991234567",
                session_path="/tmp/s",
                manager_id=1,
            )
        )
        s.add(Contact(id=10, messenger=Messenger.tg, external_user_id="999", name="Клиент"))
        s.add(
            Dialog(
                id=20,
                contact_id=10,
                messenger=Messenger.tg,
                external_chat_id="100200",
                assigned_user_id=1,
                status="active",
            )
        )
        await s.commit()

    from app.db import get_session
    from app.web.app import create_app
    from app.web.deps import get_current_manager

    app = create_app()

    async def _override_session():
        async with SessionLocal() as s:
            yield s

    async def _override_manager():
        async with SessionLocal() as s:
            res = await s.execute(select(Manager).where(Manager.id == 1))
            return res.scalar_one()

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_manager] = _override_manager

    client = TestClient(app)
    yield client, SessionLocal, tmp_path
    app.dependency_overrides.clear()
    await engine.dispose()


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def _post_png(client, dialog_id=20, caption=None):
    data = {}
    if caption is not None:
        data["caption"] = caption
    return client.post(
        f"/api/dialogs/{dialog_id}/media",
        files={"file": ("photo.png", PNG, "image/png")},
        data=data,
    )


def test_send_media_creates_message_attachment_and_outbox(media_app):
    client, SessionLocal, media_dir = media_app

    r = _post_png(client, caption="смотрите")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["direction"] == "out"
    assert body["text"] == "смотрите"
    assert body["status"] == "pending"
    assert len(body["attachments"]) == 1
    att = body["attachments"][0]
    assert att["type"] == "photo"
    assert att["mime_type"] == "image/png"
    assert att["size"] == len(PNG)
    assert att["file_name"] == "photo.png"
    assert att["file_url"] == f"/api/attachments/{att['id']}/file"

    from app.models import Attachment, OutboxItem

    async def check():
        async with SessionLocal() as s:
            attachment = (await s.execute(select(Attachment))).scalars().one()
            assert attachment.file_path.startswith("out/")
            assert (media_dir / attachment.file_path).read_bytes() == PNG
            item = (await s.execute(select(OutboxItem))).scalars().one()
            assert item.attachment_id == attachment.id
            assert item.text == "смотрите"

    import asyncio

    asyncio.run(check())


def test_send_media_placeholder_text_hidden_in_dto(media_app):
    """Без caption текст-плейсхолдер живёт в БД (для B24/превью), но DTO
    пузыря его скрывает — картинка говорит сама за себя."""
    client, SessionLocal, _ = media_app
    r = _post_png(client)
    assert r.status_code == 201
    assert r.json()["text"] is None

    from app.models import Message, OutboxItem

    async def check():
        async with SessionLocal() as s:
            msg = (await s.execute(select(Message))).scalars().one()
            assert msg.text == "[фото]"  # в БД плейсхолдер остаётся
            item = (await s.execute(select(OutboxItem))).scalars().one()
            # А в очередь уходит реальный caption (пустой) — клиенту в TG
            # плейсхолдер подписью не отправляется.
            assert item.text == ""

    import asyncio

    asyncio.run(check())


def test_send_media_svg_rejected_415(media_app):
    client, _, _ = media_app
    r = client.post(
        "/api/dialogs/20/media",
        files={"file": ("evil.svg", b"<svg onload=alert(1)>", "image/svg+xml")},
    )
    assert r.status_code == 415


def test_send_media_too_large_413(media_app, monkeypatch):
    client, _, media_dir = media_app
    # create_app уже закэшировал настройки — подменяем сам storage-синглтон
    # роута на маленький лимит (413 должен подняться до записи на диск).
    import app.web.routes.dialogs as dialogs_mod
    from app.media.storage import MediaStorage

    monkeypatch.setattr(
        dialogs_mod,
        "get_media_storage",
        lambda: MediaStorage(media_dir, max_size_bytes=8),
    )
    r = _post_png(client)  # PNG больше 8 байт
    assert r.status_code == 413


def test_send_media_max_dialog_ok(media_app):
    """Медиа канало-нейтрально: MAX-диалог проходит тот же путь, что и TG
    (раньше здесь был 409-гейт «MAX пока не поддерживается»)."""
    client, SessionLocal, _ = media_app
    from app.models import Contact, Dialog, Messenger, TgAccount

    async def seed():
        async with SessionLocal() as s:
            s.add(
                TgAccount(
                    id=8, messenger=Messenger.max, phone="max1", session_path="/tmp/m", manager_id=1
                )
            )
            s.add(Contact(id=11, messenger=Messenger.max, external_user_id="888", name="Макс"))
            s.add(
                Dialog(
                    id=21,
                    contact_id=11,
                    messenger=Messenger.max,
                    external_chat_id="777",
                    assigned_user_id=1,
                    status="active",
                )
            )
            await s.commit()

    import asyncio

    asyncio.run(seed())
    r = _post_png(client, dialog_id=21)
    assert r.status_code == 201
    body = r.json()
    assert body["attachments"], "вложение должно создаться для MAX-диалога"


def test_messages_list_includes_attachments(media_app):
    client, _, _ = media_app
    assert _post_png(client).status_code == 201
    r = client.get("/api/dialogs/20/messages")
    assert r.status_code == 200
    messages = r.json()
    att = messages[-1]["attachments"]
    assert len(att) == 1
    assert att[0]["file_url"].startswith("/api/attachments/")


def test_get_attachment_file_inline(media_app):
    client, _, _ = media_app
    att = _post_png(client).json()["attachments"][0]
    r = client.get(att["file_url"])
    assert r.status_code == 200
    assert r.content == PNG
    assert r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "private, max-age=86400"
    assert "inline" in r.headers.get("content-disposition", "")


def test_get_attachment_pdf_downloads_as_octet_stream(media_app):
    client, _, _ = media_app
    pdf = b"%PDF-1.4 fake"
    r = client.post(
        "/api/dialogs/20/media",
        files={"file": ("смета.pdf", pdf, "application/pdf")},
    )
    assert r.status_code == 201
    att = r.json()["attachments"][0]
    assert att["type"] == "file"
    r2 = client.get(att["file_url"])
    assert r2.status_code == 200
    assert r2.content == pdf
    # Вне inline-списка — octet-stream + attachment (XSS-поверхность).
    assert r2.headers["content-type"] == "application/octet-stream"
    assert "attachment" in r2.headers.get("content-disposition", "")


def test_get_attachment_foreign_manager_404(media_app):
    client, SessionLocal, _ = media_app
    att = _post_png(client).json()["attachments"][0]

    from app.models import Manager
    from app.web.deps import get_current_manager

    app = client.app

    async def _override_foreign_manager():
        async with SessionLocal() as s:
            s.add(Manager(id=2, name="Чужой", b24_user_id=16, is_active=True))
            await s.commit()
            res = await s.execute(select(Manager).where(Manager.id == 2))
            return res.scalar_one()

    app.dependency_overrides[get_current_manager] = _override_foreign_manager
    r = client.get(att["file_url"])
    assert r.status_code == 404


def test_get_attachment_missing_id_404(media_app):
    client, _, _ = media_app
    r = client.get("/api/attachments/999/file")
    assert r.status_code == 404


def _override_current_manager(app, SessionLocal, manager_id):
    """Подменяет get_current_manager на менеджера manager_id (создав его)."""
    from sqlalchemy import select

    from app.models import Manager
    from app.web.deps import get_current_manager

    async def _override():
        async with SessionLocal() as s:
            res = await s.execute(select(Manager).where(Manager.id == manager_id))
            return res.scalar_one()

    app.dependency_overrides[get_current_manager] = _override


def test_send_media_readonly_403(media_app):
    """Read-only менеджер не отправляет и медиа (общий guard контракта)."""
    client, SessionLocal, _ = media_app

    from app.models import Manager

    async def make_readonly():
        async with SessionLocal() as s:
            manager = await s.get(Manager, 1)
            manager.is_readonly = True
            await s.commit()

    import asyncio

    asyncio.run(make_readonly())
    r = _post_png(client)
    assert r.status_code == 403


def test_send_media_supervisor_foreign_dialog_403(media_app):
    """Supervisor видит чужой диалог, но медиа отправлять не может
    (известный диалог — 403, не 404)."""
    client, SessionLocal, _ = media_app

    from app.models import Manager, ManagerRole

    async def seed():
        async with SessionLocal() as s:
            s.add(
                Manager(
                    id=2, name="Надзор", b24_user_id=16, is_active=True, role=ManagerRole.supervisor
                )
            )
            await s.commit()

    import asyncio

    asyncio.run(seed())
    _override_current_manager(client.app, SessionLocal, 2)
    r = _post_png(client)  # диалог 20 принадлежит менеджеру 1
    assert r.status_code == 403
    assert "свои диалоги" in r.json()["detail"]


def test_get_attachment_supervisor_200(media_app):
    """Supervisor читает вложения чужих диалогов (контракт раздачи)."""
    client, SessionLocal, _ = media_app
    att = _post_png(client).json()["attachments"][0]

    from app.models import Manager, ManagerRole

    async def seed():
        async with SessionLocal() as s:
            s.add(
                Manager(
                    id=2, name="Надзор", b24_user_id=16, is_active=True, role=ManagerRole.supervisor
                )
            )
            await s.commit()

    import asyncio

    asyncio.run(seed())
    _override_current_manager(client.app, SessionLocal, 2)
    r = client.get(att["file_url"])
    assert r.status_code == 200
    assert r.content == PNG
