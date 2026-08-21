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
    - ``auto_reply_first_enabled``/``auto_reply_first_text`` — автоответ
      «первое входящее» (``on``/``off`` + текст; пустой текст = выключен).
    - ``auto_reply_offhours_enabled``/``auto_reply_offhours_text`` —
      автоответ «нерабочее время» (те же правила).
    - ``work_hours`` — JSON расписания автоответов
      (``{"days": [0..6], "start": "09:00", "end": "18:00"}``, Пн=0).
    - ``work_hours_tz`` — IANA-зона расписания (дефолт Europe/Moscow).
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
