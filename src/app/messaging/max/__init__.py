"""MAX-канал: эмуляция web-клиента MAX (web.max.ru) по WebSocket.

Схема воспроизводит подключение Wazzup: QR-вход «Профиль → Устройства»
создаёт долгоживущую device-сессию (token + deviceId), сообщения ходят от
имени самого менеджера. Протокол реверс-инжиниринг (ver=11, JSON-фреймы);
документация сообщества: github.com/pr0bel1230/max-api-docs + собственный
спайк scripts/spike_max_login.py (S0 пройден 2026-08-15).

Структура пакета (protocol ← ws_client ← {provider, login}; factory — сборка):

* ``protocol`` — опкоды, фреймы, типизированные ошибки cmd=3, токены;
* ``ws_client`` — транспорт: seq-матчинг, авто-pong, seam для тестов;
* ``push_parser`` — толерантный разбор входящих push'ей (единственная точка
  знания об их формате — правится по живым логам);
* ``provider`` — MaxUserProvider: долгоживущее соединение, supervise-цикл
  с backoff, heartbeat, отправка MSG_SEND;
* ``login`` — MaxQrLoginFlow: QR-онбординг (+2FA) для web-процесса;
* ``factory`` — build_max_provider для SessionManager-registry.

Импортируйте из конкретных модулей (``from app.messaging.max.provider
import MaxUserProvider``) — реэкспортов из корня нет.
"""
