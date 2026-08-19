"""Протокол канала онбординга — дверь, за которой прячется механика.

Субъект подключения — ЛИНИЯ (аккаунт): админ выдаёт share-ссылку, владелец
телефона подтверждает. Кто инициировал (supervisor/share-токен) в контракт
не входит. Что в контракт НЕ входит (защита от переусложнения):
персистенция кредов — каждый канал сам пишет свои (MAX — token+device_id
в БД, TG — .session-файл через bridge) и сам приводит аккаунт к
``status=active``.
"""

from typing import Protocol

from app.models import Messenger, TgAccount
from app.onboarding.types import LoginView


class OnboardingChannel(Protocol):
    #: Канал, который обслуживает имплементация.
    messenger: Messenger

    async def account_view(self, manager_id: int) -> dict | None:
        """Подключённый аккаунт менеджера в этом канале (или None).

        LEGACY для /admin/api/me («Мои каналы»): ищет по legacy-владельцу."""
        ...

    async def start(self, account: TgAccount, *, force: bool = False) -> dict:
        """Запустить логин линии. Аккаунт active и не force → ``already_active``.
        Повторный старт отменяет прежнюю попытку."""
        ...

    async def login_view(self, account_id: int) -> LoginView | None:
        """Состояние текущего/недавнего логина; None — показывать нечего."""
        ...

    async def submit_password(self, account_id: int, password: str) -> bool:
        """Подать 2FA-пароль; False — логин его не ждёт."""
        ...

    async def cancel(self, account_id: int) -> None:
        """Отменить текущий логин (идемпотентно)."""
        ...
