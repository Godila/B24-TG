import json

from fastapi.testclient import TestClient

from app.web.app import create_app


def test_placement_deal_sets_cookie_and_returns_html(monkeypatch):
    # Явный dev-режим: access_token фиктивный, проверка B24 пропускается.
    # (prod-путь с проверкой токена — в тестах ниже с mock _verify_b24_token.)
    monkeypatch.setenv("DEV_MODE", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    app = create_app()
    client = TestClient(app)

    form_data = {
        "PLACEMENT": "CRM_DEAL_DETAIL_TAB",
        "PLACEMENT_OPTIONS": json.dumps({"ID": "42"}),
        "AUTH": json.dumps(
            {
                "access_token": "tok",
                "user_id": "15",
                "member_id": "abc",
                "domain": "b24-x.bitrix24.ru",
                "client_endpoint": "https://b24-x.bitrix24.ru/rest/",
                "scope": "crm,im,placement",
                "expires_in": "3600",
            }
        ),
    }
    r = client.post("/placement/deal", data=form_data)

    assert r.status_code == 200
    # Session cookie set.
    cookie_header = r.headers.get("set-cookie", "")
    assert "btg_sess=" in cookie_header
    assert "HttpOnly" in cookie_header
    # HTML returned (chat page).
    assert "text/html" in r.headers.get("content-type", "")
    assert "<html" in r.text.lower()


def test_placement_deal_wrong_placement_returns_400():
    # 400 выдается до проверок auth/dev-режима — окружение не важно.
    app = create_app()
    client = TestClient(app)

    form_data = {
        "PLACEMENT": "SOME_OTHER_PLACEMENT",
        "PLACEMENT_OPTIONS": json.dumps({"ID": "42"}),
        "AUTH": json.dumps({"user_id": "15"}),
    }
    r = client.post("/placement/deal", data=form_data)
    assert r.status_code == 400


def test_placement_deal_dev_mode_works_without_auth(monkeypatch):
    """В dev-режиме можно открыть placement без реального B24 POST."""
    monkeypatch.setenv("DEV_MODE", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    app = create_app()
    client = TestClient(app)

    # GET без form-data — dev-режим должен позволить вход (для локальной разработки).
    r = client.get("/placement/deal", params={"deal_id": "42", "b24_user_id": "1"})

    assert r.status_code == 200
    cookie_header = r.headers.get("set-cookie", "")
    assert "btg_sess=" in cookie_header
    # В dev-режиме кука без Secure (http://localhost).
    assert "Secure" not in cookie_header


def test_placement_deal_prod_rejects_invalid_token(monkeypatch):
    """В prod-режиме POST с невалидным access_token → 403, кука не выставляется."""
    monkeypatch.setenv("DEV_MODE", "false")
    from app.config import get_settings
    get_settings.cache_clear()

    from unittest.mock import AsyncMock

    import app.web.routes.placement as placement_mod

    # _verify_b24_token возвращает False — токен не прошёл проверку.
    monkeypatch.setattr(
        placement_mod, "_verify_b24_token", AsyncMock(return_value=False)
    )

    app = create_app()
    client = TestClient(app)

    form_data = {
        "PLACEMENT": "CRM_DEAL_DETAIL_TAB",
        "PLACEMENT_OPTIONS": json.dumps({"ID": "42"}),
        "AUTH": json.dumps({"user_id": "15", "access_token": "bad-token"}),
    }
    r = client.post("/placement/deal", data=form_data)
    assert r.status_code == 403
    assert "btg_sess=" not in r.headers.get("set-cookie", "")


def test_placement_deal_prod_accepts_valid_token(monkeypatch):
    """В prod-режиме POST с валидным access_token → 200, кука выставляется."""
    monkeypatch.setenv("DEV_MODE", "false")
    from app.config import get_settings
    get_settings.cache_clear()

    from unittest.mock import AsyncMock

    import app.web.routes.placement as placement_mod

    monkeypatch.setattr(
        placement_mod, "_verify_b24_token", AsyncMock(return_value=True)
    )

    app = create_app()
    client = TestClient(app)

    form_data = {
        "PLACEMENT": "CRM_DEAL_DETAIL_TAB",
        "PLACEMENT_OPTIONS": json.dumps({"ID": "42"}),
        "AUTH": json.dumps({"user_id": "15", "access_token": "valid-token"}),
    }
    r = client.post("/placement/deal", data=form_data)
    assert r.status_code == 200
    cookie_header = r.headers.get("set-cookie", "")
    assert "btg_sess=" in cookie_header
    # В prod-режиме кука только по HTTPS.
    assert "Secure" in cookie_header
    # deal_id внедрён в HTML как data-deal-id.
    assert 'data-deal-id="42"' in r.text
