"""Глобальные настройки приложения (key-value, правит администратор)."""

from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, String

from app.models.base import Base


class AppSetting(Base):
    """Ключ-значение. Известные ключи:

    - ``timeline_mode`` — что писать в таймлайн CRM:
      ``all`` (каждое сообщение), ``first`` (только первое сообщение
      нового диалога), ``none`` (ничего; уведомления не трогает).
    - ``media_to_timeline`` — грузить ли вложения в timeline-комментарии
      (``on``/``off``).
    - ``crm_mode`` — какие карточки заводить новым клиентам:
      ``deal`` (контакт+сделка) или ``lead`` (только лид).
    - ``source_map`` — JSON маппинг канал→код записи справочника
      источников (``{"tg": "TELEGRAM", "max": ""}``; нет ключа — дефолт
      канала, пустая строка — источник не передавать).
    """

    __tablename__ = "app_settings"

    key = Column("key", String(64), primary_key=True)
    value = Column("value", String(255), nullable=False)
    updated_at = Column(
        "updated_at", DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
