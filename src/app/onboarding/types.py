"""Контрактные типы онбординга: единый словарь статусов и DTO для фронта."""

import enum
from dataclasses import dataclass


class OnboardingStatus(str, enum.Enum):
    """Статусная машина логина, общая для всех каналов.

    Совпадает со значениями MaxQrLoginFlow и статусами login_commands —
    фронт рендерит один компонент на любой канал.
    """

    waiting = "waiting"
    password_required = "password_required"
    authorized = "authorized"
    expired = "expired"
    error = "error"


@dataclass(slots=True)
class LoginView:
    """Состояние одного логина для API/фронта."""

    status: OnboardingStatus
    #: QR-ссылка валидна только в статусе waiting (перерисовывается при
    #: регенерации кода).
    qr_link: str | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "qr_link": self.qr_link if self.status is OnboardingStatus.waiting else None,
            "error": self.error,
        }
