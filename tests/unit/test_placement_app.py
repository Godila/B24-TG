"""Placement LEFT_MENU-оболочка /placement/app (вкладки Чаты/Панель).

Зеркалит test_placement_admin.py / test_placement_chats.py: POST —
placement-вызов (dev: AUTH-JSON), GET — фолбэк по сессионной куке.
"""

from unittest.mock import MagicMock

import pytest

from app.web.routes import placement as placement_routes


class _FakeRequest:
    """Минимальный Request: только cookies (нужно _resolve_b24_user)."""

    def __init__(self, cookies=None):
        self.cookies = cookies or {}


class _FakeSettings:
    def __init__(self, static_dir):
        self.dev_mode = True
        self.static_dir = str(static_dir)
        self.session_secret = "unit-test-secret"
        self.b24_portal = "https://unit-test.example"


@pytest.fixture
def static_dir(tmp_path):
    (tmp_path / "app-shell.html").write_text(
        "<html><body>APP-SHELL-MARKER</body></html>",
        encoding="utf-8",
    )
    return tmp_path


@pytest.mark.asyncio
async def test_app_placement_post_dev(monkeypatch, static_dir):
    monkeypatch.setattr(placement_routes, "get_settings", lambda: _FakeSettings(static_dir))
    resp = await placement_routes.placement_app_post(
        request=_FakeRequest(),
        placement="LEFT_MENU",
        auth_id="",
        auth='{"user_id": 7}',
    )
    assert resp.status_code == 200
    assert "APP-SHELL-MARKER" in resp.body.decode()
    # Кука ставится в том же ответе — важно для iFrame.
    assert "set-cookie" in {k.lower() for k in resp.headers}


@pytest.mark.asyncio
async def test_app_placement_post_wrong_code(static_dir, monkeypatch):
    monkeypatch.setattr(placement_routes, "get_settings", lambda: _FakeSettings(static_dir))
    resp = await placement_routes.placement_app_post(
        request=_FakeRequest(),
        placement="CRM_DEAL_DETAIL_TAB",
        auth_id="",
        auth="{}",
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_app_placement_post_prod_invalid_token(monkeypatch, static_dir):
    settings = _FakeSettings(static_dir)
    settings.dev_mode = False
    monkeypatch.setattr(placement_routes, "get_settings", lambda: settings)

    async def _none(token: str):
        return None

    monkeypatch.setattr(placement_routes, "_user_id_from_token", _none)
    resp = await placement_routes.placement_app_post(
        request=_FakeRequest(),
        placement="LEFT_MENU",
        auth_id="bad-token",
        auth="",
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_app_placement_get_with_manager(monkeypatch, static_dir):
    monkeypatch.setattr(placement_routes, "get_settings", lambda: _FakeSettings(static_dir))
    resp = await placement_routes.placement_app_get(_manager=MagicMock())
    assert resp.status_code == 200
    assert "APP-SHELL-MARKER" in resp.body.decode()


def _signed_cookie(user_id: int, ttl: int = 3600) -> dict[str, str]:
    from app.web.session import SESSION_COOKIE, create_session_payload, sign_session

    token = sign_session(create_session_payload(user_id, ttl=ttl), "unit-test-secret")
    return {SESSION_COOKIE: token}


@pytest.mark.asyncio
async def test_app_post_cookie_skip_only_without_auth_id(monkeypatch, static_dir):
    """AUTH_ID пуст (перезагрузка фрейма) + живая кука → user.current
    пропускается. AUTH_ID непустой — токен авторитетен, кука не подменяет
    личность (общий браузер / смена аккаунта B24)."""
    settings = _FakeSettings(static_dir)
    settings.dev_mode = False
    monkeypatch.setattr(placement_routes, "get_settings", lambda: settings)
    placement_routes._TOKEN_CACHE.clear()

    calls = []

    async def _check(token: str):
        calls.append(token)
        return 7

    monkeypatch.setattr(placement_routes, "_user_id_from_token", _check)

    # Пустой AUTH_ID: кука принята, B24-проверки не было.
    resp = await placement_routes.placement_app_post(
        request=_FakeRequest(cookies=_signed_cookie(7)),
        placement="LEFT_MENU",
        auth_id="",
        auth="",
    )
    assert resp.status_code == 200
    assert calls == []

    # Непустой AUTH_ID (реальный placement-POST): полный путь по токену,
    # даже при живой куке другого пользователя.
    resp2 = await placement_routes.placement_app_post(
        request=_FakeRequest(cookies=_signed_cookie(999)),
        placement="LEFT_MENU",
        auth_id="b24-token",
        auth="",
    )
    assert resp2.status_code == 200
    assert calls == ["b24-token"]


@pytest.mark.asyncio
async def test_user_id_from_token_caches_result(monkeypatch, static_dir):
    """Сам кэш: первый вызов идёт в B24, второй — из _TOKEN_CACHE."""
    settings = _FakeSettings(static_dir)
    settings.dev_mode = False
    monkeypatch.setattr(placement_routes, "get_settings", lambda: settings)
    placement_routes._TOKEN_CACHE.clear()

    b24_calls = []

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def call(self, method, auth_token=None):
            b24_calls.append(auth_token)
            return {"ID": 7}

        async def aclose(self):
            pass

    monkeypatch.setattr(placement_routes, "Bitrix24Client", _FakeClient)

    assert await placement_routes._user_id_from_token("tok-1") == 7
    assert await placement_routes._user_id_from_token("tok-1") == 7
    assert b24_calls == ["tok-1"]  # второй раз — из кэша
    placement_routes._TOKEN_CACHE.clear()


@pytest.mark.asyncio
async def test_static_html_versions_static_refs_only(monkeypatch, tmp_path):
    """?v=<mtime> дописывается на /static-ссылки, /placement/* не трогается."""
    (tmp_path / "page.html").write_text(
        '<link rel="stylesheet" href="/static/style.css">'
        '<script defer src="/static/vendor/alpine.min.js"></script>'
        '<iframe src="/placement/chats"></iframe>',
        encoding="utf-8",
    )
    (tmp_path / "style.css").write_text("body{}", encoding="utf-8")
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "alpine.min.js").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(placement_routes, "get_settings", lambda: _FakeSettings(tmp_path))

    html = placement_routes._static_html("page.html", "t", "stub")
    assert 'href="/static/style.css?v=' in html
    assert 'src="/static/vendor/alpine.min.js?v=' in html
    assert 'src="/placement/chats"' in html
