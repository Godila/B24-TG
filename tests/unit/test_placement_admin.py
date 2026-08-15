"""Placement LEFT_MENU: админ-панель внутри интерфейса B24.

POST — placement-вызов (dev: AUTH-JSON), GET — фолбэк по сессионной куке.
"""

from unittest.mock import MagicMock

import pytest

from app.web.routes import placement as placement_routes


class _FakeSettings:
    def __init__(self, static_dir):
        self.dev_mode = True
        self.static_dir = str(static_dir)
        self.session_secret = "unit-test-secret"


@pytest.fixture
def static_dir(tmp_path):
    (tmp_path / "admin.html").write_text(
        "<html><body>ADMIN-PAGE-MARKER</body></html>", encoding="utf-8",
    )
    return tmp_path


@pytest.mark.asyncio
async def test_admin_placement_post_dev(monkeypatch, static_dir):
    monkeypatch.setattr(
        placement_routes, "get_settings", lambda: _FakeSettings(static_dir)
    )
    resp = await placement_routes.placement_admin_post(
        placement="LEFT_MENU", auth_id="", auth='{"user_id": 7}',
    )
    assert resp.status_code == 200
    assert "ADMIN-PAGE-MARKER" in resp.body.decode()
    # Кука ставится в том же ответе — важно для iFrame.
    assert "set-cookie" in {k.lower() for k in resp.headers}


@pytest.mark.asyncio
async def test_admin_placement_post_wrong_code(static_dir, monkeypatch):
    monkeypatch.setattr(
        placement_routes, "get_settings", lambda: _FakeSettings(static_dir)
    )
    resp = await placement_routes.placement_admin_post(
        placement="CRM_DEAL_DETAIL_TAB", auth_id="", auth="{}",
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_admin_placement_post_prod_invalid_token(
    monkeypatch, static_dir
):
    settings = _FakeSettings(static_dir)
    settings.dev_mode = False
    monkeypatch.setattr(
        placement_routes, "get_settings", lambda: settings
    )

    async def _none(token: str):
        return None

    monkeypatch.setattr(placement_routes, "_user_id_from_token", _none)
    resp = await placement_routes.placement_admin_post(
        placement="LEFT_MENU", auth_id="bad-token", auth="",
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_placement_get_with_manager(monkeypatch, static_dir):
    monkeypatch.setattr(
        placement_routes, "get_settings", lambda: _FakeSettings(static_dir)
    )
    resp = await placement_routes.placement_admin_get(_manager=MagicMock())
    assert resp.status_code == 200
    assert "ADMIN-PAGE-MARKER" in resp.body.decode()
