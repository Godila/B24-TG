"""Канал WhatsApp: OpenWA-сайдкар (NestJS-шлюз над Baileys).

Слои как у MAX: ``api`` (REST-команды), ``events`` (Socket.IO /events —
живые события), ``provider`` (MessengerProvider, паттерн MAX-очередей),
``media`` (скачивание входящих в MediaStorage; отправка — base64 из файла),
``factory`` (сборка из строки аккаунта + Settings). Протокол —
reverse-engineered multi-device; контракт — спека OpenWA docs/06
(локальная копия: .zcode/tmp/openwa-api-spec.md).
"""
