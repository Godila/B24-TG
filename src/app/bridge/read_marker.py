"""ReadMarker — перевод исходящих сообщений диалога в status=read.

Канало-нейтральный консьюмер ReadReceipt (подписка — bootstrap.forward_reads):
диалог по тройке (messenger, external_chat_id, assigned_user_id), затем
монотонный UPDATE только sent/delivered → read. Без очереди/CRM-эффектов:
переход идемпотентен фильтром статуса, курсоры кумулятивны — потерянная
квитанция закрывается следующей (клиент снова откроет чат). Гонка «квитанция
раньше mark_sent» (строка ещё pending) фильтром статусов пропускается и
тоже закрывается следующим курсором канала.
"""

import logging
from collections.abc import Callable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.messaging.types import ReadReceipt
from app.models import Dialog, Message, MessageDirection, MessageStatus

logger = logging.getLogger(__name__)

#: Только эти статусы переводятся в read (монотонность: pending/error/read
#: не трогаем; delivered у inbound исключён фильтром direction).
_READABLE = (MessageStatus.sent, MessageStatus.delivered)


class ReadMarker:
    """ReadReceipt → Message.status='read' для исходящих диалога."""

    def __init__(self, db_session_factory: Callable[[], AsyncSession]):
        self._db_factory = db_session_factory

    async def apply(self, receipt: ReadReceipt, *, account) -> int:
        """Применить квитанцию; вернуть число переведённых строк.

        Диалог ищется по линии аккаунта (✓✓ — свойство номера: чей личный
        номер или общий — клиент прочёл наши исходящие). Не найден
        (группа/чужая линия) — 0, штатно: группы не инжестятся, квитанция
        до чужого провайдера не доходит.
        """
        async with self._db_factory() as session:
            dialog = (
                await session.execute(
                    select(Dialog)
                    .where(
                        Dialog.messenger == receipt.messenger,
                        Dialog.external_chat_id == receipt.external_chat_id,
                        Dialog.account_id == account.id,
                    )
                    .order_by(Dialog.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if dialog is None:
                logger.debug(
                    "read-квитанция без диалога (messenger=%s chat=%s)",
                    receipt.messenger.value,
                    receipt.external_chat_id,
                )
                return 0
            rows = (
                await session.execute(
                    select(Message.id, Message.external_message_id).where(
                        Message.dialog_id == dialog.id,
                        Message.direction == MessageDirection.outbound,
                        Message.status.in_(_READABLE),
                    )
                )
            ).all()
            ids = [mid for mid, ext in rows if self._matches(ext, receipt.up_to_external_id)]
            if ids:
                await session.execute(
                    update(Message).where(Message.id.in_(ids)).values(status=MessageStatus.read)
                )
                await session.commit()
                logger.info(
                    "read: %s chat=%s → %d msg(s)",
                    receipt.messenger.value,
                    receipt.external_chat_id,
                    len(ids),
                )
            else:
                # Идемпотентный повтор (reconnect-реплей квитанций MAX) —
                # не событие для INFO.
                logger.debug(
                    "read: %s chat=%s → no-op (курсор уже закрыт)",
                    receipt.messenger.value,
                    receipt.external_chat_id,
                )
            return len(ids)

    @staticmethod
    def _matches(external_message_id: str | None, up_to: int | None) -> bool:
        """Попадает ли сообщение под курсор квитанции.

        ``up_to=None`` (MAX: курсор чата) — все. Иначе числовое сравнение
        (TG: id MTProto): лексическое ловило бы «100» < «9»; нечисловой
        или пустой id — False (не попадает под курсор — остаётся sent,
        закроется следующим курсором).
        """
        if up_to is None:
            return True
        if external_message_id is None:
            return False
        try:
            return int(external_message_id) <= up_to
        except ValueError:
            return False
