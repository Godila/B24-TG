"""Онбординг канальных аккаунтов — процесс-нейтральный контракт.

Web-роуты знают только этот контракт; механика исполнения различается:
MAX-флоу живёт в web-процессе (токен в БД), TG — в bridge (команды через
таблицу login_commands, т.к. .session-файл имеет единственного писателя).
Оба канала сходятся в ``tg_accounts.status=active`` — дальше аккаунт
подхватывает AccountSyncWorker (bridge).
"""

from app.onboarding.protocol import OnboardingChannel
from app.onboarding.types import LoginView, OnboardingStatus

__all__ = ["LoginView", "OnboardingChannel", "OnboardingStatus"]
