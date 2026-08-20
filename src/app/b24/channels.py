"""Канал-профили Bitrix24: тексты и SOURCE_ID по мессенджеру.

Единая точка параметризации B24-артефактов, которые раньше были захардкожены
под Telegram: текст уведомления, источник карточек CRM. Канал в НАЗВАНИЕ
сделки/лида больше не пишется — только в поле «Источник» (SOURCE_ID).

SOURCE_ID должен существовать в справочнике портала (crm.status.source);
ни TELEGRAM, ни MAX стандартными НЕ являются — оба добавляются
scripts/add_max_source.py (пока записи нет, карточки создаются без
источника, фолбэк пишет WARNING в лог).
"""

from dataclasses import dataclass

from app.models import Messenger


@dataclass(frozen=True, slots=True)
class B24ChannelProfile:
    notify_label: str
    source_id: str | None


CHANNEL_PROFILES: dict[Messenger, B24ChannelProfile] = {
    Messenger.tg: B24ChannelProfile(
        notify_label="Telegram",
        source_id="telegram",
    ),
    Messenger.max: B24ChannelProfile(
        notify_label="MAX",
        source_id="MAX",
    ),
}


def channel_profile(messenger: Messenger) -> B24ChannelProfile:
    # KeyError вместо молчаливого TG-профиля: новый канал без профиля
    # должен упасть заметно, а не притворяться Telegram.
    return CHANNEL_PROFILES[messenger]
