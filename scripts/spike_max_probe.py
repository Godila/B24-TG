#!/usr/bin/env python3
"""Дебаг-проба WS-эндпоинтов MAX: сырой трафик, коды закрытия, версии протокола."""
import asyncio
import json
import sys

import websockets

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
INIT_PAYLOAD = {
    "deviceId": "11111111-2222-3333-4444-555555555555",
    "userAgent": {
        "deviceType": "WEB",
        "pushDeviceType": "WEBPUSH",
        "locale": "ru",
        "deviceLocale": "ru",
        "osVersion": "Windows",
        "deviceName": "Chrome",
        "headerUserAgent": UA,
        "isPwa": False,
        "appVersion": "26.8.4",
        "screen": "1080x1920 1.0x",
        "timezone": "Europe/Moscow",
    },
}


async def probe(url: str, ver: int, seq: int) -> None:
    print(f"\n=== {url}  ver={ver} seq={seq} ===", flush=True)
    try:
        ws = await websockets.connect(
            url,
            additional_headers={"Origin": "https://web.max.ru", "User-Agent": UA},
            open_timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        print("CONNECT FAIL:", type(exc).__name__, str(exc)[:200], flush=True)
        return
    print("connected", flush=True)
    frame = json.dumps({"ver": ver, "cmd": 0, "seq": seq, "opcode": 6, "payload": INIT_PAYLOAD})
    try:
        await ws.send(frame)
        print("sent INIT", flush=True)
        for i in range(3):
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=8)
            except TimeoutError:
                print(f"recv[{i}]: TIMEOUT (8s)", flush=True)
                break
            preview = repr(msg)[:400] if isinstance(msg, (str, bytes)) else repr(msg)
            print(f"recv[{i}]: {type(msg).__name__} {preview}", flush=True)
    except websockets.ConnectionClosed as exc:
        rcvd = getattr(exc, "rcvd", None)
        print("CLOSED BY SERVER: code=", getattr(rcvd, "code", "?"), "reason=", getattr(rcvd, "reason", "?"), flush=True)
    finally:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


async def main() -> None:
    for url in ("wss://api.oneme.ru/websocket", "wss://ws-api.oneme.ru/websocket"):
        for ver in (11,):
            await probe(url, ver, 1)
    # эксперимент: тот же новый URL, но seq как у клиента (0-based nextOutSeq?) — нет,
    # seq=1 уже соответствует дока-описанию; отдельная проба ver=12 на новом URL:
    await probe("wss://api.oneme.ru/websocket", 12, 1)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main())
