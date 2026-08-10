from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.web.app import create_app


def test_onappinstall_saves_token():
    app = create_app()
    client = TestClient(app)

    payload = {
        "event": "ONAPPINSTALL",
        "data": {"VERSION": "1", "LANGUAGE_ID": "ru"},
        "ts": "1700000000",
        "auth": {
            "access_token": "new_access",
            "expires_in": "3600",
            "scope": "crm,im",
            "domain": "b24-ye2jjz.bitrix24.ru",
            "client_endpoint": "https://b24-ye2jjz.bitrix24.ru/rest/",
            "member_id": "test_member_123",
            "refresh_token": "new_refresh",
            "user_id": 1,
        },
    }
    with patch("app.web.routes.webhook.get_token_manager") as mock_get:
        tm = AsyncMock()
        tm.save_install_data = AsyncMock()
        mock_get.return_value = tm
        response = client.post("/webhook/b24/onappinstall", json=payload)

    assert response.status_code == 200
    tm.save_install_data.assert_awaited_once()
