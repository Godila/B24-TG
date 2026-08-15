"""Протокол канала онбординга — дверь, за которой прячется механика.

Что в контракт НЕ входит (защита от переусложнения): персистенция кредов.
Каждый канал сам пишет свои (MAX — token+device_id в БД, TG — .session-файл
через bridge) и сам приводит аккаунт к ``status=active``.
"""

from typing import Protocol

from app.models import Manager, Messenger
from app.onboarding.types import LoginView


class OnboardingChannel(Protocol):
    #: Канал, который обслуживает имплементация.
    messenger: Messenger

    async def account_view(self, manager_id: int) -> dict | None:
        """Подключённый аккаунт менеджера в этом канале (или None)."""
        ...

    async def start(self, manager: Manager, *, force: bool = False) -> dict:
        """Запустить логин. Аккаунт active и не force → ``already_active``.
        Повторный старт отменяет прежнюю попытку."""
        ...

    async def login_view(self, manager_id: int) -> LoginView | None:
        """Состояние текущего/недавнего логина; None — показывать нечего."""
        ...

    async def submit_password(self, manager_id: int, password: str) -> bool:
        """Подать 2FA-пароль; False — логин его не ждёт."""
        ...

    async def cancel(self, manager_id: int) -> None:
        """Отменить текущий логин (идемпотентно)."""
        ...
