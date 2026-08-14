"""Прокси для Telethon: хостинги РФ часто блокируют MTProto-подсети.

Telethon принимает tuple в стиле PySocks:
``(proxy_type, host, port, rdns, username, password)`` — тип можно строкой
("socks5" | "socks4" | "http", см. ``telethon/network/connection/connection.py::
_parse_proxy``). Для async-режима нужна библиотека ``python-socks[asyncio]``.
"""

import logging

from app.config import Settings

logger = logging.getLogger(__name__)

_SCHEMES = ("socks5", "socks4", "http")


def telethon_proxy(settings: Settings) -> tuple | None:
    """Собрать proxy-tuple для TelegramClient из настроек.

    Пустая схема/хост/порт → None (подключение напрямую).
    Неизвестная схема — ValueError: silently ignored прокси здесь опаснее,
    чем падение на старте (сессия «молча» не подключится).
    """
    scheme = settings.tg_proxy_scheme.strip().lower()
    if not scheme and not settings.tg_proxy_host:
        return None
    if scheme not in _SCHEMES:
        raise ValueError(
            f"Неизвестная схема TG-прокси: {settings.tg_proxy_scheme!r} "
            f"(ожидаются {'|'.join(_SCHEMES)} или пусто)"
        )
    if not settings.tg_proxy_host or not settings.tg_proxy_port:
        raise ValueError(
            "TG_PROXY_SCHEME задан, но TG_PROXY_HOST/TG_PROXY_PORT пусты — "
            "укажите полный адрес прокси или очистите схему"
        )
    proxy = (
        scheme,
        settings.tg_proxy_host,
        settings.tg_proxy_port,
        True,  # rdns: имена резолвим на стороне прокси
        settings.tg_proxy_username or None,
        settings.tg_proxy_password or None,
    )
    logger.info("Telethon будет подключаться через %s-прокси %s:%s",
                scheme, settings.tg_proxy_host, settings.tg_proxy_port)
    return proxy
