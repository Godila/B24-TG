#!/usr/bin/env python3
"""Spike S0: QR-логин в MAX через WebSocket-протокол web-клиента.

Проверяет воспроизводимость схемы Wazzup: эмуляция веб-устройства MAX
(web.max.ru). Протокол ver=11 (JSON-фреймы) по реверс-документации
github.com/pr0bel1230/max-api-docs.

Команды:
  login                     INIT -> QR_AUTH_REQUEST -> QR-картинка -> скан
                            телефоном -> токен -> LOGIN; сессия пишется в
                            spike_max_session.json (repo root, в .gitignore)
  chats [--count N]         список чатов по сохранённой сессии
  send <chatId> <текст>     тестовая отправка сообщения
  resume                    проверить, что сохранённый токен ещё жив
  watch [--duration сек]    soak-тест: держим сессию, отвечаем на heartbeat,
                            логируем push'ы; по умолчанию 3600 сек

Грабли протокола (из reconnect.md):
  - держим ОДНО соединение на сессию: ~30-50 LOGIN одним токеном за короткое
    время -> сервер сбрасывает токен;
  - простой ~30-60 сек -> сервер рвёт соединение: отвечаем на серверный
    ping (push opcode 1) и шлём свой раз в 15 сек при тишине;
  - реконнект с backoff 2,4,8,16,32 сек.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path

import websockets

BASE = Path(__file__).resolve().parent.parent
SESSION_FILE = BASE / "spike_max_session.json"

# Рабочий эндпоинт — легаси ws-api (текстовый JSON, ver=11). Новый
# wss://api.oneme.ru/websocket (бинарный фрейминг, binaryType=arraybuffer)
# закрывает текстовые соединения — не исследован, не нужен.
# ГРАБЛИ: QR включён только при СВОЕВРЕМЕННОМ appVersion (25.11.1 из доков
# -> "qr_login.disabled"; 26.8.4 -> ок). При дрейфе версий web-клиента
# обновлять USER_AGENT по бандлам web.max.ru.
WS_URL = "wss://ws-api.oneme.ru/websocket"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HEADERS = {"Origin": "https://web.max.ru", "User-Agent": BROWSER_UA}

OP_PING = 1
OP_INIT = 6
OP_LOGIN = 19
OP_GET_CHATS = 53
OP_MSG_SEND = 64
OP_QR_AUTH_REQUEST = 288
OP_QR_AUTH_POLL = 289
# 291/115 выцеплены из бандла web-клиента (chunk CJji1lxl.js): реальный флоу —
# поллинг 289 возвращает status.loginAvailable, затем 291 {trackId} завершает
# вход (ответ: profile; passwordChallenge при 2FA -> 115 {trackId, password}).
# «push opcode 18 с токеном» из реверс-доков — неточность (18/CODE_ENTER —
# SMS-флоу).
OP_QR_AUTH_LOGIN = 291
OP_QR_PASSWORD = 115

# INIT payload воспроизводит текущий web-клиент (выцеплено из бандла web.max.ru
# 2026-08: appVersion 26.8.4, поле isPwa; headerUserAgent = реальный UA браузера).
USER_AGENT = {
    "deviceType": "WEB",
    "pushDeviceType": "WEBPUSH",
    "locale": "ru",
    "deviceLocale": "ru",
    "osVersion": "Windows",
    "deviceName": "Chrome",
    "headerUserAgent": BROWSER_UA,
    "isPwa": False,
    "appVersion": "26.8.4",
    "screen": "1080x1920 1.0x",
    "timezone": "Europe/Moscow",
}

log = logging.getLogger("spike")


def _extract_token(payload: dict) -> str | None:
    """Токен приходит в push opcode 18: payload.tokenAttrs.LOGIN.token."""
    attrs = payload.get("tokenAttrs")
    if not isinstance(attrs, dict):
        return None
    tok = ((attrs.get("LOGIN") or {}).get("token")) if isinstance(attrs.get("LOGIN"), dict) else None
    return tok or None


class MaxWsClient:
    """Минимальный клиент WS-протокола MAX: seq-матчинг + push-обработка."""

    def __init__(self) -> None:
        self._ws = None
        self._seq = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self._open = False
        self.token: str | None = None
        self.token_event = asyncio.Event()
        self.push_count = 0
        self.last_send = time.monotonic()

    @property
    def closed(self) -> bool:
        return not self._open

    async def connect(self) -> None:
        # websockets>=12: additional_headers; старые версии: extra_headers.
        try:
            self._ws = await websockets.connect(WS_URL, additional_headers=HEADERS)
        except TypeError:
            self._ws = await websockets.connect(WS_URL, extra_headers=HEADERS)
        self._open = True
        self._reader = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        self._open = False
        if self._reader:
            self._reader.cancel()
        if self._ws:
            await self._ws.close()

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def _send_raw(self, frame: dict) -> None:
        await self._ws.send(json.dumps(frame, ensure_ascii=False))
        self.last_send = time.monotonic()

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                frame = json.loads(raw)
                cmd = frame.get("cmd")
                seq = frame.get("seq")
                if cmd in (1, 3) and seq in self._pending:
                    fut = self._pending.pop(seq)
                    if not fut.done():
                        fut.set_result(frame)
                else:
                    await self._handle_push(frame)
        except websockets.ConnectionClosed as exc:
            log.info("WS соединение закрыто: %s", exc)
        except Exception:
            log.exception("reader упал")
        finally:
            self._open = False
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("ws closed"))
            self._pending.clear()

    async def _handle_push(self, frame: dict) -> None:
        op = frame.get("opcode")
        payload = frame.get("payload") or {}
        self.push_count += 1
        if op == OP_PING:
            # Серверный heartbeat: отвечаем interactive- pong'ом.
            await self._send_raw(
                {
                    "ver": 11,
                    "cmd": 0,
                    "seq": self._next_seq(),
                    "opcode": OP_PING,
                    "payload": {"interactive": True},
                }
            )
            return
        tok = _extract_token(payload)
        if op == 18 and tok:
            self.token = tok
            self.token_event.set()
            return
        snippet = json.dumps(payload, ensure_ascii=False)[:160]
        log.info("push op=%s: %s", op, snippet)

    async def request(self, opcode: int, payload: dict | None = None, timeout: float = 15.0) -> dict:
        """Отправить запрос и дождаться ответа с совпадающим seq."""
        seq = self._next_seq()
        frame = {"ver": 11, "cmd": 0, "seq": seq, "opcode": opcode, "payload": payload or {}}
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[seq] = fut
        await self._send_raw(frame)
        try:
            resp = await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(seq, None)
        if resp.get("cmd") == 3:
            err = json.dumps(resp.get("payload"), ensure_ascii=False)[:300]
            raise RuntimeError(f"opcode {opcode} cmd=3: {err}")
        return resp


async def ws_init(c: MaxWsClient, device_id: str) -> dict:
    return await c.request(OP_INIT, {"deviceId": device_id, "userAgent": USER_AGENT})


async def ws_login(c: MaxWsClient, token: str) -> dict:
    return await c.request(
        OP_LOGIN,
        {
            "token": token,
            "interactive": True,
            "chatsCount": 20,
            "chatsSync": 20,
            "contactsSync": 0,
            "presenceSync": 0,
            "draftsSync": 0,
        },
        timeout=20.0,
    )


def render_qr_png(link: str, idx: int) -> Path:
    """Каждый QR — в СВОЙ файл: Windows Photos не перезагружает изменённый
    файл, и юзер рискует сканировать устаревшую картинку."""
    import qrcode

    path = BASE / f"spike_max_qr_{idx}.png"
    img = qrcode.make(link)
    img.save(path)
    return path


def open_png(path: Path) -> None:
    try:
        os.startfile(path)
    except Exception:
        log.warning("Не удалось открыть %s автоматически — откройте вручную", path)


class QrPageServer:
    """Одна страница в браузере с автообновлением QR каждые 3 сек.

    Вместо всплывающих окон просмотрщика на каждую перегенерацию (они
    плодились и путали пользователя) — одна вкладка на весь процесс.
    """

    def __init__(self, holder: dict, port: int = 8765) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        self._holder = holder
        holder_ref = holder

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/qr.png":
                    p = holder_ref.get("path")
                    if p and Path(p).exists():
                        data = Path(p).read_bytes()
                        self.send_response(200)
                        self.send_header("Content-Type", "image/png")
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                    else:
                        self.send_response(404)
                        self.end_headers()
                    return
                body = (
                    b"<!doctype html><html><head><meta charset='utf-8'>"
                    b"<meta http-equiv='refresh' content='3'>"
                    b"<title>MAX QR</title></head>"
                    b"<body style='margin:0;display:flex;align-items:center;"
                    b"justify-content:center;min-height:100vh;background:#111'>"
                    b"<img src='/qr.png' alt='QR' style='max-width:90vh;"
                    b"max-width:90vw'></body></html>"
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self._srv = HTTPServer(("127.0.0.1", port), Handler)
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._srv.shutdown()


def print_login_summary(login_resp: dict) -> dict:
    payload = login_resp.get("payload") or {}
    profile = payload.get("profile") or {}
    name = " ".join(filter(None, [profile.get("firstName"), profile.get("lastName")]))
    name = name or profile.get("nick") or "?"
    phones = profile.get("phones") or profile.get("phoneNumbers") or []
    chats = payload.get("chats") or []
    print(f"=== LOGIN ok: {name} (user_id={profile.get('id')}, телефонов в профиле: {len(phones)}) ===")
    print(f"=== Чатов в ответе LOGIN: {len(chats)} ===")
    for ch in chats[:10]:
        print(f"  chatId={ch.get('id')}  {ch.get('title') or ch.get('type') or ''}")
    return payload


async def cmd_login(args: argparse.Namespace) -> int:
    device_id = str(uuid.uuid4())
    c = MaxWsClient()
    await c.connect()
    init = await ws_init(c, device_id)
    log.info("INIT ok (cmd=%s)", init.get("cmd"))

    qr = await c.request(OP_QR_AUTH_REQUEST)
    payload = qr.get("payload") or {}
    link = payload.get("qrLink")
    track_id = payload.get("trackId")
    interval = (payload.get("pollingInterval") or 5000) / 1000.0
    if not link or not track_id:
        print("Неожиданный ответ QR_AUTH_REQUEST:", json.dumps(payload, ensure_ascii=False)[:400])
        return 2
    log.info(
        "QR получен (ttl=%s, expiresAt=%s, poll каждые %.1fс)",
        payload.get("ttl"), payload.get("expiresAt"), interval,
    )

    qr_idx = 1
    png = render_qr_png(link, qr_idx)
    holder = {"path": png}
    page = QrPageServer(holder)
    page.start()
    import webbrowser

    print("\n" + "=" * 64)
    print("Одна вкладка браузера с QR открыта (страница сама обновляет код).")
    print("Если вкладка не открылась — зайдите вручную: http://127.0.0.1:8765/")
    print("Телефон: MAX -> Профиль -> Устройства -> Войти по QR-коду.")
    print("=" * 64 + "\n", flush=True)
    webbrowser.open("http://127.0.0.1:8765/")
    try:
        return await _login_flow(c, args, device_id, link, track_id, interval, qr_idx, holder)
    finally:
        page.stop()


async def _login_flow(
    c: MaxWsClient,
    args: argparse.Namespace,
    device_id: str,
    link: str | None,
    track_id: str | None,
    interval: float,
    qr_idx: int,
    holder: dict,
) -> int:
    deadline = time.monotonic() + args.timeout
    auth_payload: dict | None = None
    while time.monotonic() < deadline and auth_payload is None:
        try:
            resp = await c.request(OP_QR_AUTH_POLL, {"trackId": track_id}, timeout=10.0)
        except RuntimeError as exc:
            if "track.not.found" in str(exc):
                qr_idx += 1
                log.warning("QR истёк/использован — запрашиваю новый (#%d)", qr_idx)
                qr = await c.request(OP_QR_AUTH_REQUEST)
                payload = qr.get("payload") or {}
                link, track_id = payload.get("qrLink"), payload.get("trackId")
                if link and track_id:
                    holder["path"] = render_qr_png(link, qr_idx)
                    print(f">>> QR #{qr_idx} обновлён на странице <<<", flush=True)
                continue
            raise
        status = ((resp.get("payload") or {}).get("status")) or {}
        if status.get("loginAvailable"):
            print("Телефон подтвердил вход! Завершаю авторизацию (opcode 291)…", flush=True)
            auth_resp = await c.request(OP_QR_AUTH_LOGIN, {"trackId": track_id}, timeout=20.0)
            auth_payload = auth_resp.get("payload") or {}
            if auth_payload.get("passwordChallenge"):
                print("Аккаунт защищён паролем (2FA). ", flush=True)
                pwd = await asyncio.to_thread(input, "Введите пароль двухфакторки: ")
                auth_resp = await c.request(
                    OP_QR_PASSWORD, {"trackId": track_id, "password": pwd}, timeout=20.0
                )
                auth_payload = auth_resp.get("payload") or {}
            break
        log.info(
            "poll: status=%s", json.dumps(status, ensure_ascii=False)[:160] or "(пусто)"
        )
        await asyncio.sleep(interval)

    if auth_payload is None:
        print(f"Время ожидания истекло ({args.timeout:.0f}с) — вход не подтверждён.")
        await c.close()
        return 2

    print("\n=== Ответ авторизации (первые 1200 символов) ===")
    print(json.dumps(auth_payload, ensure_ascii=False)[:1200])
    print("=" * 40, flush=True)

    # Токены для resume ищем рекурсивно по ключам с "token".
    tokens: dict[str, str] = {}

    def _walk(obj: object, path: str = "") -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                p = f"{path}.{k}" if path else str(k)
                if isinstance(v, str) and "token" in str(k).lower():
                    tokens[p] = v
                else:
                    _walk(v, p)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                _walk(v, f"{path}[{i}]")

    _walk(auth_payload)
    if tokens:
        print("Найдены токены:")
        for p, v in tokens.items():
            print(f"  {p}: {v[:12]}… (len={len(v)})")
    else:
        print("Явных токенов в ответе нет — сессия, вероятно, привязана к соединению/deviceId.")

    profile = (auth_payload.get("profile") or {})
    contact = (profile.get("contact") or {})
    user_id = contact.get("id")

    # Сразу проверяем, что сессия рабочая: тянем чаты через это же соединение.
    chats_preview: list[dict] = []
    try:
        chats_resp = await c.request(
            OP_GET_CHATS, {"count": 10, "marker": int(time.time() * 1000)}
        )
        chats_preview = (chats_resp.get("payload") or {}).get("chats") or []
        print(f"=== GET_CHATS ok: {len(chats_preview)} чатов ===")
        for ch in chats_preview[:10]:
            print(f"  chatId={ch.get('id')}  {ch.get('title') or ch.get('type') or ''}")
    except Exception as exc:  # noqa: BLE001
        print(f"GET_CHATS не удался: {exc}")

    SESSION_FILE.write_text(
        json.dumps(
            {
                "device_id": device_id,
                "track_id": track_id,
                "user_id": user_id,
                "tokens": tokens,
                "auth_payload": auth_payload,
                "login_at": int(time.time()),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    name = " ".join(filter(None, [contact.get("firstName"), contact.get("lastName")])) or contact.get("nick") or "?"
    print(f"\n=== ВЫПОЛНЕНО: {name} (user_id={user_id}) ===")
    print(f"Сессия сохранена: {SESSION_FILE.name} (НЕ коммитить — в .gitignore)")
    print("Дальше: chats / send <chatId> <текст> / resume / watch --duration 86400")
    await c.close()
    return 0


def load_session() -> dict:
    if not SESSION_FILE.exists():
        print("Нет spike_max_session.json — сначала выполните `login`")
        sys.exit(2)
    return json.loads(SESSION_FILE.read_text(encoding="utf-8"))


def _pick_token(data: dict) -> str | None:
    """Выбрать токен для повторного входа из сохранённой сессии."""
    tokens = data.get("tokens") or {}
    if not tokens:
        return data.get("token")  # легаси-формат
    best: tuple[int, str] | None = None
    for path, value in tokens.items():
        lp = path.lower()
        score = 2 if ("login" in lp or "access" in lp) else 1
        if best is None or score > best[0] or (score == best[0] and len(value) > len(best[1])):
            best = (score, value)
    return best[1] if best else None


async def open_saved_session() -> tuple[MaxWsClient, dict, dict]:
    data = load_session()
    token = _pick_token(data)
    if not token:
        raise RuntimeError(
            "В spike_max_session.json нет токена — сессия была привязана к соединению; нужен повторный login"
        )
    c = MaxWsClient()
    await c.connect()
    await ws_init(c, data["device_id"])
    login_resp = await ws_login(c, token)
    return c, data, login_resp


async def cmd_chats(args: argparse.Namespace) -> int:
    c, _, _ = await open_saved_session()
    resp = await c.request(OP_GET_CHATS, {"count": args.count, "marker": int(time.time() * 1000)})
    chats = (resp.get("payload") or {}).get("chats") or []
    print(f"=== GET_CHATS: {len(chats)} ===")
    for ch in chats:
        print(f"  chatId={ch.get('id')}  {ch.get('title') or ch.get('type') or ''}")
    await c.close()
    return 0


async def cmd_send(args: argparse.Namespace) -> int:
    c, _, _ = await open_saved_session()
    resp = await c.request(
        OP_MSG_SEND,
        {
            "chatId": args.chat_id,
            "message": {
                "text": args.text,
                "cid": int(time.time() * 1000),
                "elements": [],
                "attaches": [],
            },
            "notify": True,
        },
    )
    msg = (resp.get("payload") or {}).get("message") or {}
    print(f"Отправлено: message.id={msg.get('id')} (time={msg.get('time')})")
    await c.close()
    return 0


async def cmd_resume(args: argparse.Namespace) -> int:
    try:
        c, data, login_resp = await open_saved_session()
    except RuntimeError as exc:
        print(f"СЕССИЯ НЕ ВОССТАНАВЛИВАЕТСЯ: {exc}")
        return 1
    age_h = (time.time() - data.get("login_at", time.time())) / 3600
    print(f"СЕССИЯ ЖИВА: LOGIN ok, возраст токена ~{age_h:.1f} ч")
    print_login_summary(login_resp)
    await c.close()
    return 0


async def cmd_watch(args: argparse.Namespace) -> int:
    data = load_session()
    token = _pick_token(data)
    if not token:
        print("В сессии нет токена — watch невозможен, выполните login.")
        return 1
    started = time.monotonic()
    c: MaxWsClient | None = None
    backoff = 2
    reconnects = 0
    while time.monotonic() - started < args.duration:
        try:
            if c is None or c.closed:
                c = MaxWsClient()
                await c.connect()
                await ws_init(c, data["device_id"])
                await ws_login(c, token)
                log.info("сессия поднята (LOGIN ok), реконнектов: %d", reconnects)
                backoff = 2
            # Тишина >15с — шлём свой ping (протокол требует активности).
            if time.monotonic() - c.last_send > 15:
                await c.request(OP_PING, {"interactive": True}, timeout=10.0)
            await asyncio.sleep(2)
        except Exception as exc:
            reconnects += 1
            log.warning("обрыв (%s) — реконнект через %dс", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 32)
            c = None
    elapsed = time.monotonic() - started
    pushes = c.push_count if c else 0
    print(f"watch завершён: {elapsed/3600:.1f} ч, реконнектов: {reconnects}, push'ов: {pushes}")
    if c:
        await c.close()
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("login", help="QR-вход, сохранение сессии")
    p.add_argument("--timeout", type=float, default=300.0, help="сколько ждать сканирования, сек")

    p = sub.add_parser("chats", help="список чатов по сессии")
    p.add_argument("--count", type=int, default=20)

    p = sub.add_parser("send", help="тестовая отправка")
    p.add_argument("chat_id", type=int)
    p.add_argument("text")

    sub.add_parser("resume", help="проверить, что токен жив")

    p = sub.add_parser("watch", help="soak-тест сессии")
    p.add_argument("--duration", type=float, default=3600.0, help="сек (86400 = сутки)")

    args = parser.parse_args()
    handlers = {
        "login": cmd_login,
        "chats": cmd_chats,
        "send": cmd_send,
        "resume": cmd_resume,
        "watch": cmd_watch,
    }
    return asyncio.run(handlers[args.command](args))


if __name__ == "__main__":
    sys.exit(main())
