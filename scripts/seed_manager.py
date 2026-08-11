#!/usr/bin/env python3
"""Seed: создаёт менеджера + TgAccount-заглушку (нужен для get_current_manager
и send-эндпоинта). Запускается внутри web-контейнера:

    docker compose exec web python /app/scripts/seed_manager.py

b24_user_id=1 — это админ-аккаунт портала (тот, что ставил приложение).
TgAccount — заглушка: phone/session_path пустые, status=offline. Реальный
номер подключается позже (заменяется при первом auth-логине).
"""
import asyncio

from sqlalchemy import select

from app.db import async_session
from app.models import Manager, ManagerRole, TgAccount, TgAccountStatus

B24_USER_ID = 1
MANAGER_NAME = "Админ"
TG_PHONE = "+70000000000"  # заглушка, заменяется при подключении реального номера


async def main():
    async with async_session() as s:
        mgr = (
            await s.execute(select(Manager).where(Manager.b24_user_id == B24_USER_ID))
        ).scalar_one_or_none()
        if mgr is None:
            mgr = Manager(
                name=MANAGER_NAME,
                b24_user_id=B24_USER_ID,
                role=ManagerRole.supervisor,
                is_active=True,
            )
            s.add(mgr)
            await s.flush()
            print(f"Created Manager id={mgr.id} b24_user_id={B24_USER_ID}")
        else:
            print(f"Manager already exists id={mgr.id}")

        acc = (
            await s.execute(select(TgAccount).where(TgAccount.manager_id == mgr.id))
        ).scalar_one_or_none()
        if acc is None:
            acc = TgAccount(
                phone=TG_PHONE,
                session_path="/data/tg_sessions/account_1/session",
                status=TgAccountStatus.offline,
                manager_id=mgr.id,
            )
            s.add(acc)
            await s.flush()
            print(f"Created TgAccount id={acc.id} (placeholder, offline)")
        else:
            print(f"TgAccount already exists id={acc.id}")

        await s.commit()
        print("Seed complete.")


if __name__ == "__main__":
    asyncio.run(main())
