"""push_parser: разбор входящих push'ей MAX по РЕАЛЬНО пойманному формату.

Кадр пойман soak'ом 2026-08-15 14:22 (push op=128, обновление чата):
    {"chatId": 422733600, "unread": 1, "chat": {..., "type": "DIALOG",
     "lastMessage": {"sender": 248843813, "id": "117099261900910729",
                     "time": 1786792936720, "text": "Геор", "type": "USER",
                     "attaches": [], "reactionInfo": {}}}}
"""

from app.messaging.max.push_parser import parse_message_push


def _frame(payload: dict, opcode: int = 128) -> dict:
    return {"ver": 11, "cmd": 0, "seq": 100, "opcode": opcode, "payload": payload}


def _chat_payload(
    *,
    chat_id: int = 422733600,
    chat_type: str = "DIALOG",
    sender: int = 248843813,
    text: str = "Геор",
    msg_id="117099261900910729",
    time_ms: int = 1786792936720,
    attaches=None,
    msg_type: str = "USER",
) -> dict:
    last_message = {
        "sender": sender,
        "id": msg_id,
        "time": time_ms,
        "text": text,
        "type": msg_type,
        "attaches": attaches or [],
        "reactionInfo": {},
    }
    return {
        "chatId": chat_id,
        "unread": 1,
        "chat": {"id": chat_id, "type": chat_type, "lastMessage": last_message},
    }


def test_real_caught_frame_parses():
    parsed = parse_message_push(_frame(_chat_payload()), own_user_id=401041669)
    assert parsed.skip_reason is None
    assert parsed.self_message is False
    assert parsed.external_chat_id == "422733600"
    assert parsed.sender_external_id == "248843813"
    assert parsed.external_message_id == "117099261900910729"
    assert parsed.text == "Геор"
    assert parsed.timestamp is not None and parsed.timestamp.year == 2026
    assert parsed.content_type.value == "text"
    assert parsed.is_reply is False


def test_self_message_marked_not_skipped():
    """Self-пуш больше не скип: эхо/устройство решает провайдер (эхо-сет)."""
    parsed = parse_message_push(_frame(_chat_payload()), own_user_id=248843813)
    assert parsed.skip_reason is None
    assert parsed.self_message is True
    # Поля распарсены полностью — нужны device-outbound инжесту.
    assert parsed.external_message_id == "117099261900910729"
    assert parsed.text == "Геор"
    assert parsed.timestamp is not None


def test_self_in_group_skipped_by_group_gate():
    """Групп-гейт парсера стоит ВЫШЕ self-метки: self-пуш из группы скипается."""
    parsed = parse_message_push(_frame(_chat_payload(chat_type="GROUP")), own_user_id=248843813)
    assert parsed.skip_reason == "group_group"
    assert parsed.self_message is False


def test_self_light_push_marked():
    """Лёгкая форма self-пуша (payload.message) — тоже помечается."""
    payload = {
        "chatId": 53007183,
        "unread": 0,
        "message": {
            "sender": 401041669,
            "id": "117106696678555000",
            "time": 1786906382424,
            "text": "написал с телефона",
            "type": "USER",
            "attaches": [],
        },
    }
    parsed = parse_message_push(_frame(payload), own_user_id=401041669)
    assert parsed.skip_reason is None
    assert parsed.self_message is True
    assert parsed.chat_type_known is False


def test_self_service_message_still_skipped():
    """Служебные типы (SERVICE) скипаются и для своих сообщений."""
    parsed = parse_message_push(_frame(_chat_payload(msg_type="SERVICE")), own_user_id=248843813)
    assert parsed.skip_reason == "service_service"


def test_group_chat_skipped():
    parsed = parse_message_push(_frame(_chat_payload(chat_type="GROUP")), own_user_id=1)
    assert parsed.skip_reason == "group_group"


def test_favorites_skipped():
    parsed = parse_message_push(_frame(_chat_payload(chat_id=0)), own_user_id=1)
    assert parsed.skip_reason == "favorites"


def test_service_message_skipped():
    parsed = parse_message_push(_frame(_chat_payload(msg_type="SERVICE")), own_user_id=1)
    assert parsed.skip_reason == "service_service"


def test_activity_push_skipped():
    parsed = parse_message_push(_frame({"chatId": 1, "userId": 2}, opcode=129), own_user_id=1)
    assert parsed.skip_reason == "activity"


def test_unknown_opcode_skipped():
    parsed = parse_message_push(_frame({"strange": 1}, opcode=777), own_user_id=1)
    assert parsed.skip_reason == "op_777"


def test_numeric_message_id_becomes_str():
    parsed = parse_message_push(_frame(_chat_payload(msg_id=117099065741753584)), own_user_id=1)
    assert parsed.external_message_id == "117099065741753584"


def test_attach_image_without_text_placeholder():
    payload = _chat_payload(text="", attaches=[{"type": "IMAGE"}])
    parsed = parse_message_push(_frame(payload), own_user_id=1)
    assert parsed.content_type.value == "photo"
    assert parsed.text == "[фото]"


def test_attach_voice_placeholder():
    payload = _chat_payload(text="", attaches=[{"type": "AUDIO"}])
    parsed = parse_message_push(_frame(payload), own_user_id=1)
    assert parsed.content_type.value == "voice"
    assert parsed.text == "[голосовое сообщение]"


def test_no_last_message_skipped():
    parsed = parse_message_push(_frame({"chatId": 5, "chat": {"type": "DIALOG"}}), own_user_id=1)
    assert parsed.skip_reason == "no_message"


def test_reply_detected():
    payload = _chat_payload()
    payload["chat"]["lastMessage"]["replyTo"] = "12345"
    parsed = parse_message_push(_frame(payload), own_user_id=1)
    assert parsed.is_reply is True


# --- Лёгкая форма пуша (поймана живьём 2026-08-16 на e2e) --------------- #


def test_light_push_payload_message_parsed():
    """2-е+ сообщения чата: {chatId, unread, message:{...}} без chat-объекта.

    Раньше парсер читал только chat.lastMessage — эти сообщения терялись."""
    payload = {
        "chatId": 53007183,
        "unread": 1,
        "message": {
            "sender": 349157962,
            "id": "117106696678554959",
            "time": 1786906382424,
            "text": "Геор ты угадал",
            "type": "USER",
            "attaches": [],
        },
    }
    parsed = parse_message_push(_frame(payload), own_user_id=401041669)
    assert parsed.skip_reason is None
    assert parsed.external_chat_id == "53007183"
    assert parsed.sender_external_id == "349157962"
    assert parsed.external_message_id == "117106696678554959"
    assert parsed.text == "Геор ты угадал"
    # Тип чата в лёгкой форме неизвестен — провайдер проверит CHAT_INFO.
    assert parsed.chat_type_known is False


def test_full_push_chat_type_known():
    parsed = parse_message_push(_frame(_chat_payload()), own_user_id=1)
    assert parsed.skip_reason is None
    assert parsed.chat_type_known is True


# --- Хелперы контакта (GET_CONTACTS, формат пойман живьём 2026-08-16) --- #


def test_contact_display_name_prefers_full_name():
    from app.messaging.max.push_parser import contact_display_name

    contact = {
        "names": [
            {"name": "Тимур", "type": "ONEME"},
            {"name": "Тимур Азизов", "type": "FULL_NAME"},
        ]
    }
    assert contact_display_name(contact) == "Тимур Азизов"


def test_contact_display_name_first_last_fallback():
    from app.messaging.max.push_parser import contact_display_name

    assert (
        contact_display_name({"names": [{"firstName": "Тимур", "lastName": "Азизов"}]})
        == "Тимур Азизов"
    )
    assert contact_display_name({"names": []}) is None


def test_contact_phone_extracted():
    from app.messaging.max.push_parser import contact_phone

    contact = {"phones": [{"type": "MOBILE", "number": "+79990001122"}]}
    assert contact_phone(contact) == "+79990001122"
    assert contact_phone({"phones": []}) is None


def test_contact_name_parts_prefers_full_name_entry():
    from app.messaging.max.push_parser import contact_name_parts

    contact = {
        "names": [
            {"firstName": "Т.", "type": "ONEME"},
            {"firstName": "Тимур", "lastName": "Азизов", "type": "FULL_NAME"},
        ]
    }
    assert contact_name_parts(contact) == ("Тимур", "Азизов")


def test_contact_name_parts_first_available_entry():
    from app.messaging.max.push_parser import contact_name_parts

    assert contact_name_parts({"names": [{"firstName": "Тимур", "type": "ONEME"}]}) == (
        "Тимур",
        None,
    )
    assert contact_name_parts({"names": [{"name": "Тимур"}]}) == (None, None)
    assert contact_name_parts({}) == (None, None)


# --- Вложения: толерантная нормализация MaxAttach ----------------------- #
# Живые кадры с непустыми attaches не пойманы — парсер обязан переварить
# варианты имён из реверс-моделей комьюнити (type/_type, baseUrl/url,
# name/filename, size/fileSize, подтаблицы photo/file/video).


def test_attach_type_variant_and_url():
    payload = _chat_payload(
        text="",
        attaches=[{"_type": "PHOTO", "baseUrl": "https://i.oneme.ru/i?r=1", "photoId": 33}],
    )
    parsed = parse_message_push(_frame(payload), own_user_id=1)
    assert parsed.skip_reason is None
    assert parsed.content_type.value == "photo"
    assert parsed.text == "[фото]"
    assert parsed.attach is not None
    assert parsed.attach.kind == "PHOTO"
    assert parsed.attach.url == "https://i.oneme.ru/i?r=1"
    assert parsed.attach.raw["photoId"] == 33  # сырой кадр хранит всё


def test_attach_tolerant_field_names():
    payload = _chat_payload(
        text="",
        attaches=[
            {
                "type": "IMAGE",
                "filename": "shot.PNG",
                "fileSize": "123",
                "mimeType": "image/png; charset=binary",
            }
        ],
    )
    parsed = parse_message_push(_frame(payload), own_user_id=1)
    attach = parsed.attach
    assert attach.kind == "PHOTO"  # IMAGE* → PHOTO (нормализация)
    assert attach.file_name == "shot.PNG"
    assert attach.size == 123
    assert attach.mime == "image/png"  # params срезаны


def test_attach_nested_subtable():
    payload = _chat_payload(
        text="",
        attaches=[{"type": "FILE", "file": {"fileId": "77", "url": "https://fd.oneme.ru/d"}}],
    )
    parsed = parse_message_push(_frame(payload), own_user_id=1)
    assert parsed.attach.file_id == 77
    assert parsed.attach.url == "https://fd.oneme.ru/d"
    # Сырой кадр сохранён для форензики смоука.
    assert parsed.attach.raw["type"] == "FILE"


def test_attach_video_with_ids():
    payload = _chat_payload(text="", attaches=[{"_type": "VIDEO", "videoId": 5, "duration": 12}])
    parsed = parse_message_push(_frame(payload), own_user_id=1)
    assert parsed.content_type.value == "video"
    assert parsed.text == "[видео]"
    assert parsed.attach.video_id == 5


def test_attach_sticker_placeholder_no_download_path():
    """Стикер — отдельный плейсхолдер (как TG), скачивания нет."""
    parsed = parse_message_push(
        _frame(_chat_payload(text="", attaches=[{"type": "STICKER"}])), own_user_id=1
    )
    assert parsed.content_type.value == "sticker"
    assert parsed.text == "[стикер]"
    assert parsed.attach.kind == "STICKER"


def test_attach_inline_keyboard_is_file():
    parsed = parse_message_push(
        _frame(_chat_payload(text="", attaches=[{"_type": "INLINE_KEYBOARD", "buttons": []}])),
        own_user_id=1,
    )
    assert parsed.content_type.value == "file"
    assert parsed.text == "[вложение]"


def test_attach_broken_element_still_placeholder():
    """Битой элемент attaches (не dict) — файл-плейсхолдер, как раньше."""
    parsed = parse_message_push(_frame(_chat_payload(text="", attaches=["мусор"])), own_user_id=1)
    assert parsed.content_type.value == "file"
    assert parsed.text == "[файл]"
    assert parsed.attach is not None and parsed.attach.kind == "FILE"


def test_text_message_has_no_attach():
    parsed = parse_message_push(_frame(_chat_payload()), own_user_id=401041669)
    assert parsed.attach is None


def test_attach_caption_preserved_over_placeholder():
    payload = _chat_payload(text="наш отчёт", attaches=[{"type": "FILE", "fileId": 3}])
    parsed = parse_message_push(_frame(payload), own_user_id=1)
    assert parsed.text == "наш отчёт"
    assert parsed.content_type.value == "file"
    assert parsed.attach.file_id == 3
