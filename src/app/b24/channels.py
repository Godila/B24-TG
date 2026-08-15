"""Канал-профили Bitrix24: тексты и SOURCE_ID по мессенджеру.

Единая точка параметризации B24-артефактов, которые раньше были захардкожены
под Telegram: префикс заголовка сделки, текст уведомления, источник контакта.

SOURCE_ID должен существовать в справочнике портала (crm.status.source);
у «TELEGRAM» он стандартный, у «MAX» — нет, поэтому пока не передаём
(контакт получит источник по умолчанию). Задать свой источник MAX можно
в админке B24 и затем поменять здесь.
"""

from dataclasses import dataclass

from app.models import Messenger


@dataclass(frozen=True, slots=True)
class B24ChannelProfile:
    deal_prefix: str
    notify_label: str
    source_id: str | None


CHANNEL_PROFILES: dict[Messenger, B24ChannelProfile] = {
    Messenger.tg: B24ChannelProfile(
        deal_prefix="TG: ", notify_label="Telegram", source_id="telegram",
    ),
    Messenger.max: B24ChannelProfile(
        deal_prefix="MAX: ", notify_label="MAX", source_id=None,
    ),
}


def channel_profile(messenger: Messenger) -> B24ChannelProfile:
    # KeyError вместо молчаливого TG-профиля: новый канал без профиля
    # должен упасть заметно, а не притворяться Telegram.
    return CHANNEL_PROFILES[messenger]
