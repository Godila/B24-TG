"""Интеграционные тесты webhook ONAPPINSTALL: секрет + валидация auth payload."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

WEBHOOK_SECRET = "test-webhook-secret-123"

AUTH_PAYLOAD = {
    "access_token": "new_access",
    "expires_in": "3600",
    "scope": "crm,im",
    "domain": "b24-ye2jjz.bitrix24.ru",
    "client_endpoint": "https://b24-ye2jjz.bitrix24.ru/rest/",
    "member_id": "test_member_123",
    "refresh_token": "new_refresh",
    "user_id": "1",
}


@pytest.fixture
def client(monkeypatch):
    # Единственный override над базой из conftest: секрет webhook.
    monkeypatch.setenv("B24_WEBHOOK_SECRET", WEBHOOK_SECRET)
    from app.config import get_settings
    get_settings.cache_clear()
    from app.web.app import create_app
    return TestClient(create_app())


def test_onappinstall_without_secret_header_returns_401(client):
    """Без заголовка и с непрошедшим self-check токена — 401, не сохраняем."""
    with patch("app.web.routes.webhook.get_token_manager") as mock_get, \
         patch("app.web.routes.webhook._token_belongs_to_portal",
               new=AsyncMock(return_value=False)):
        tm = AsyncMock()
        tm.save_install_data = AsyncMock()
        mock_get.return_value = tm
        response = client.post(
            "/webhook/b24/onappinstall", json={"event": "ONAPPINSTALL", "auth": AUTH_PAYLOAD},
        )

    assert response.status_code == 401
    tm.save_install_data.assert_not_awaited()


def test_onappinstall_without_header_selfcheck_ok_saves(client):
    """Реальный вызов с портала: заголовка нет, но токен валиден на endpoint
    payload → 200 и токены сохранены (self-check через user.current)."""
    with patch("app.web.routes.webhook.get_token_manager") as mock_get, \
         patch("app.web.routes.webhook._token_belongs_to_portal",
               new=AsyncMock(return_value=True)):
        tm = AsyncMock()
        tm.save_install_data = AsyncMock()
        mock_get.return_value = tm
        response = client.post(
            "/webhook/b24/onappinstall", json={"event": "ONAPPINSTALL", "auth": AUTH_PAYLOAD},
        )

    assert response.status_code == 200
    tm.save_install_data.assert_awaited_once()


def test_onappinstall_unknown_auth_fields_tolerated(client):
    """Живой кейс 08-22: переустановка после выдачи новых прав прислала в auth
    незнакомое поле — forbid валил установку 422, токен с новыми scope
    терялся. Лишние поля игнорируем."""
    payload = {**AUTH_PAYLOAD, "some_new_b24_field": "value"}
    with patch("app.web.routes.webhook.get_token_manager") as mock_get,          patch("app.web.routes.webhook._token_belongs_to_portal",
               new=AsyncMock(return_value=True)):
        tm = AsyncMock()
        tm.save_install_data = AsyncMock()
        mock_get.return_value = tm
        response = client.post(
            "/webhook/b24/onappinstall", json={"event": "ONAPPINSTALL", "auth": payload},
        )

    assert response.status_code == 200
    tm.save_install_data.assert_awaited_once()


def test_onappinstall_with_wrong_secret_returns_401(client):
    """Неверное значение секрета и битый токен (self-check fail) — тоже 401."""
    with patch("app.web.routes.webhook._token_belongs_to_portal",
               new=AsyncMock(return_value=False)):
        response = client.post(
            "/webhook/b24/onappinstall",
            json={"event": "ONAPPINSTALL", "auth": AUTH_PAYLOAD},
            headers={"X-Webhook-Secret": "wrong-secret"},
        )
        assert response.status_code == 401


def test_onappinstall_with_secret_skips_selfcheck(client):
    """Верный заголовок — self-check не нужен (ручной доверенный путь)."""
    with patch("app.web.routes.webhook.get_token_manager") as mock_get, \
         patch("app.web.routes.webhook._token_belongs_to_portal",
               new=AsyncMock(return_value=False)) as selfcheck:
        tm = AsyncMock()
        tm.save_install_data = AsyncMock()
        mock_get.return_value = tm
        response = client.post(
            "/webhook/b24/onappinstall",
            json={"event": "ONAPPINSTALL", "auth": AUTH_PAYLOAD},
            headers={"X-Webhook-Secret": WEBHOOK_SECRET},
        )

    assert response.status_code == 200
    selfcheck.assert_not_awaited()


def test_onappinstall_with_secret_saves_token(client):
    """Валидный секрет + полный auth → 200, save_install_data вызван."""
    with patch("app.web.routes.webhook.get_token_manager") as mock_get:
        tm = AsyncMock()
        tm.save_install_data = AsyncMock()
        mock_get.return_value = tm
        response = client.post(
            "/webhook/b24/onappinstall",
            json={"event": "ONAPPINSTALL", "auth": AUTH_PAYLOAD},
            headers={"X-Webhook-Secret": WEBHOOK_SECRET},
        )

    assert response.status_code == 200
    tm.save_install_data.assert_awaited_once()
    # Строковые user_id/expires_in приведены схемой к int.
    saved = tm.save_install_data.await_args.args[0]
    assert saved["user_id"] == 1
    assert saved["expires_in"] == 3600
    assert saved["member_id"] == "test_member_123"


def test_onappinstall_invalid_auth_returns_422(client):
    """Секрет верный, но auth неполный (нет refresh_token) → 422, токены НЕ сохраняются."""
    broken_auth = {k: v for k, v in AUTH_PAYLOAD.items() if k != "refresh_token"}
    with patch("app.web.routes.webhook.get_token_manager") as mock_get:
        tm = AsyncMock()
        tm.save_install_data = AsyncMock()
        mock_get.return_value = tm
        response = client.post(
            "/webhook/b24/onappinstall",
            json={"event": "ONAPPINSTALL", "auth": broken_auth},
            headers={"X-Webhook-Secret": WEBHOOK_SECRET},
        )

    assert response.status_code == 422
    tm.save_install_data.assert_not_awaited()


def test_onappinstall_malformed_json_returns_422(client):
    """Секрет верный, но тело — не JSON → 422 (не 500), токены НЕ сохраняются."""
    with patch("app.web.routes.webhook.get_token_manager") as mock_get:
        tm = AsyncMock()
        tm.save_install_data = AsyncMock()
        mock_get.return_value = tm
        response = client.post(
            "/webhook/b24/onappinstall",
            content="not-json{",
            headers={"X-Webhook-Secret": WEBHOOK_SECRET},
        )

    assert response.status_code == 422
    tm.save_install_data.assert_not_awaited()


def test_onappinstall_form_encoded_body_saves(client):
    """Реальный вызов с портала (живой лог 08-20): form-urlencoded с
    php-массивами auth[access_token]=… — раньше отвергался как «не JSON»."""
    with patch("app.web.routes.webhook.get_token_manager") as mock_get, \
         patch("app.web.routes.webhook._token_belongs_to_portal",
               new=AsyncMock(return_value=True)):
        tm = AsyncMock()
        tm.save_install_data = AsyncMock()
        mock_get.return_value = tm
        response = client.post(
            "/webhook/b24/onappinstall",
            content=(
                "event=ONAPPINSTALL"
                "&auth[access_token]=new_access"
                "&auth[expires_in]=3600"
                "&auth[scope]=crm,im,bizproc"
                "&auth[domain]=b24-ye2jjz.bitrix24.ru"
                "&auth[client_endpoint]=https://b24-ye2jjz.bitrix24.ru/rest/"
                "&auth[member_id]=test_member_123"
                "&auth[refresh_token]=new_refresh"
                "&auth[user_id]=1"
            ),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    assert response.status_code == 200
    saved = tm.save_install_data.await_args.args[0]
    assert saved["access_token"] == "new_access"
    assert saved["member_id"] == "test_member_123"
    assert saved["user_id"] == 1  # строка приведена схемой к int


# ---------------------------------------------------------------------- #
# Авто-регистрация чат-бота при установке с правом imbot
# ---------------------------------------------------------------------- #
def test_onappinstall_registers_imbot_when_scope_present(client, monkeypatch):
    """Право imbot в scope установки → бот регистрируется автоматически,
    id сохраняется в app_settings (нулевое ручное сопровождение)."""
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.example")
    from app.config import get_settings

    get_settings.cache_clear()
    calls = []

    async def fake_ensure(client_, token_, *, webhook_url):
        calls.append(webhook_url)
        return 25

    saved = []

    async def fake_save(bot_id):
        saved.append(bot_id)

    async def fake_saved():
        return False

    with patch("app.web.routes.webhook.ensure_bot_registered", new=fake_ensure),          patch("app.web.routes.webhook._imbot_saved", new=fake_saved),          patch("app.web.routes.webhook._save_imbot_id", new=fake_save),          patch("app.web.routes.webhook.get_token_manager") as mock_get,          patch("app.web.routes.webhook._token_belongs_to_portal",
               new=AsyncMock(return_value=True)):
        tm = AsyncMock()
        tm.save_install_data = AsyncMock()
        mock_get.return_value = tm
        payload = {**AUTH_PAYLOAD, "scope": "crm,im,imbot"}
        response = client.post(
            "/webhook/b24/onappinstall", json={"event": "ONAPPINSTALL", "auth": payload}
        )

    assert response.status_code == 200
    assert calls == ["https://app.example/webhook/b24/imbot"]
    assert saved == [25]


def test_onappinstall_skips_imbot_without_scope(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.example")

    async def fake_ensure(client_, token_, *, webhook_url):  # pragma: no cover
        raise AssertionError("не должен вызываться без imbot-права")

    with patch("app.web.routes.webhook.ensure_bot_registered", new=fake_ensure),          patch("app.web.routes.webhook.get_token_manager") as mock_get,          patch("app.web.routes.webhook._token_belongs_to_portal",
               new=AsyncMock(return_value=True)):
        tm = AsyncMock()
        tm.save_install_data = AsyncMock()
        mock_get.return_value = tm
        payload = {**AUTH_PAYLOAD, "scope": "crm,im"}
        response = client.post(
            "/webhook/b24/onappinstall", json={"event": "ONAPPINSTALL", "auth": payload}
        )

    assert response.status_code == 200
