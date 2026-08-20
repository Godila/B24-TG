"""Хендлер активити бизнес-процессов Битрикс24 «ЧатМост: отправить сообщение».

Wazzup-паритет: шаг в конструкторе БП/роботов → B24 POST-ит на
/webhook/b24/bizproc подставленные значения ({=Document:...} разрешает сам
B24 до вызова) → ставим сообщение в outbox → воркер отправляет в мессенджер.
Активити регистрируется с USE_SUBSCRIPTION='N': 200 = шаг завершён,
доставку тянут ретраи outbox (fire-and-forget, записи в журнал БП нет).

Адресация v1 — «последний диалог» CRM-сущности из нашей БД (сделка/лид/контакт;
у компании нет пути к диалогам в модели — честная ошибка шага). Нет диалога
или линии → не-200: шаг красный в журнале БП, менеджер видит кого не достали.
Файл — первая [https://…] в тексте: скачивается в медиа-том и уходит
вложением, остальной текст — подпись; http-ссылки и прочие [ссылки]
остаются текстом (вложением — только публичный https).

Формат payload хендлера в доках B24 не описан — парсинг толерантный
(JSON + form, кандидаты-поля), сырой payload (без токенов) в логе:
первый живой прогон уточнит адаптер. Авторизация: X-Webhook-Secret (ручной
путь) ИЛИ member_id из payload == порталу из БД + self-check токена
user.current на НАШЕМ portal-URL из конфига (не на endpoint из payload —
его контролирует отправитель).
"""

import asyncio
import hmac
import ipaddress
import json
import logging
import re
import socket
import time
import urllib.parse
from dataclasses import dataclass
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bridge.outbox_repo_sqlalchemy import SqlAlchemyOutboxRepository
from app.config import get_settings
from app.db import get_session
from app.media.storage import (
    StoredFile,
    attachment_type_for,
    ext_for,
    get_media_storage,
    mime_allowed_for_upload,
    normalize_mime,
    sanitize_file_name,
)
from app.messaging.types import MEDIA_PLACEHOLDERS
from app.models import (
    Attachment,
    B24Token,
    Contact,
    Dialog,
    Message,
    MessageDirection,
    MessageStatus,
    TgAccount,
    TgAccountStatus,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook/b24", tags=["bizproc"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]

#: Первая [https-ссылка] в тексте — кандидатом на вложение; http-ссылки
#: остаются текстом (их отвергнет SSRF-фильтр — шаг не должен падать из-за
#: не-https ссылки в тексте).
_LINK_RE = re.compile(r"\[(https://[^\s\[\]]+)\]")
#: document_id-суффикс B24: ['crm', 'CCrmDocumentDeal', 'DEAL_123'].
_DOCUMENT_RE = re.compile(r"^(LEAD|DEAL|CONTACT|COMPANY)_(\d+)$", re.IGNORECASE)

_MAX_BODY_BYTES = 256 * 1024
_MAX_TEXT_LEN = 4096  # паритет SendMessageIn
#: Подпись медиа у каналов ограничена (TG: 1024) — резать на отправке поздно
#: (шаг уже зелёный), честный не-200 лучше тихого failed в outbox.
_MAX_CAPTION_LEN = 1024

# Позитивный кэш self-check: бёрст БП по N сущностям шлёт один и тот же
# access_token — 1 сетевой user.current на бёрст. Негативный НЕ кэшируем
# (сбой сети не должен превращать 401 в sticky).
_SELFCHECK_TTL = 300.0
_selfcheck_ok: dict[str, float] = {}

# ponytail: дедуб event_token в памяти процесса — закрывает бёрст-ретраи
# очереди B24, не переживает рестарт; персистентная таблица, если дубли
# увидим в живую.
_EVENT_TTL = 3600.0
_seen_events: dict[str, float] = {}

_ENTITY_LABELS = {"deal": "сделки", "lead": "лида", "contact": "контакта"}


@dataclass(frozen=True, slots=True)
class ActivityRequest:
    """Распарсенный вызов активити (fail-closed: мусор → None у парсера)."""

    entity_type: str  # 'deal' | 'lead' | 'contact' | 'company'
    entity_id: int
    text: str
    event_token: str | None
    access_token: str | None
    member_id: str | None
    user_id: int | None


def _payload_dict(body: bytes, content_type: str) -> dict | None:
    """Тело вызова → dict: JSON, иначе form-urlencoded (php-ключи a[b])."""
    if not body or len(body) > _MAX_BODY_BYTES:
        return None
    # B24-очередь может не ставить content-type — JSON опознаём и по '{'.
    if "json" in content_type.lower() or body.lstrip()[:1] == b"{":
        try:
            data = json.loads(body)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None
    if "form" in content_type.lower():
        # ponytail: form-ветка — до живого прогона (формат payload B24 в доках
        # не описан); если очередь шлёт JSON — вырезать вместе с кандидатами.
        root: dict = {}
        indexed: dict[str, list[str]] = {}
        for key, value in urllib.parse.parse_qsl(
            body.decode("utf-8", "replace"), keep_blank_values=True
        ):
            m = re.fullmatch(r"(\w+)\[(\w+)\]", key)
            if m:
                # PHP-массив B24: a[0]/a[1]/… — список, иначе вложенный dict.
                if m.group(2).isdigit():
                    indexed.setdefault(m.group(1), []).append(value)
                else:
                    root.setdefault(m.group(1), {})[m.group(2)] = value
            else:
                root[key] = value
        root.update(indexed)
        return root
    return None


def _first_str(value: object) -> str | None:
    """B24 любит оборачивать значения списком — берём первую строку."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                return item
    return None


def _parse_document(doc: object) -> tuple[str, int] | None:
    """document_id (['crm','CCrmDocumentLead','LEAD_5'] | 'LEAD_5') → (kind, id)."""
    if isinstance(doc, (list, tuple)):
        doc = doc[-1] if doc else None
    if not isinstance(doc, str):
        return None
    m = _DOCUMENT_RE.match(doc.strip())
    return (m.group(1).lower(), int(m.group(2))) if m else None


def _activity_request(payload: dict) -> ActivityRequest | None:
    """Извлечь сущность+текст+auth; None = битый вызов (422, сырой лог уже есть)."""
    properties = payload.get("properties")
    if not isinstance(properties, dict):
        properties = {}
    text = _first_str(properties.get("message", payload.get("message")))
    if not text or len(text) > _MAX_TEXT_LEN:
        return None
    entity = _parse_document(payload.get("document_id", payload.get("DOCUMENT_ID")))
    if entity is None:
        return None

    auth = payload.get("auth") if isinstance(payload.get("auth"), dict) else payload

    def _s(key: str) -> str | None:
        v = auth.get(key)
        return v if isinstance(v, str) and v else None

    raw_user = auth.get("user_id", auth.get("USER_ID"))
    try:
        user_id = int(raw_user) if raw_user else None
    except (TypeError, ValueError):
        user_id = None
    event_token = _first_str(payload.get("event_token"))
    return ActivityRequest(
        entity_type=entity[0],
        entity_id=entity[1],
        text=text,
        event_token=event_token,
        access_token=_s("access_token"),
        member_id=_s("member_id"),
        user_id=user_id,
    )


def _redacted(payload: dict) -> str:
    """Сырой payload для лога: токены вырезаем (правило: секреты не в логи).
    Маскируем и корневые ключи с "token" — form-payload несёт их плоско."""
    safe = {k: "***" if "token" in k.lower() else v for k, v in payload.items()}
    auth = safe.get("auth")
    if isinstance(auth, dict):
        safe["auth"] = {k: "***" if "token" in k.lower() else v for k, v in auth.items()}
    text = json.dumps(safe, ensure_ascii=False, default=str)
    return text[:4000]


async def _token_alive(access_token: str) -> bool:
    """Self-check: токен жив на НАШЕМ портале (settings.b24_portal)."""
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                settings.b24_portal.rstrip("/") + "/rest/user.current",
                params={"auth": access_token},
            )
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        logger.warning("BIZPROC: self-check токена не прошёл (сеть/не-JSON)")
        return False
    return resp.status_code == 200 and isinstance(data.get("result"), dict)


async def _authorized(
    secret_header: str | None, ar: ActivityRequest, session: AsyncSession
) -> bool:
    """Эшелоны: секрет-заголовок → member_id-сверка с БД → self-check токена."""
    settings = get_settings()
    if secret_header and settings.b24_webhook_secret and hmac.compare_digest(
        secret_header.encode("utf-8"), settings.b24_webhook_secret.encode("utf-8")
    ):
        return True
    if not ar.access_token or not ar.member_id:
        return False
    token_row = (await session.execute(select(B24Token).limit(1))).scalar_one_or_none()
    if token_row is None or token_row.member_id != ar.member_id:
        return False
    now = time.monotonic()
    if _selfcheck_ok.get(ar.access_token, 0.0) > now:
        return True
    if await _token_alive(ar.access_token):
        _selfcheck_ok[ar.access_token] = now + _SELFCHECK_TTL
        return True
    return False


def _is_recent_event(event_token: str | None) -> bool:
    """True = такой вызов уже завершался успехом недавно (дубль)."""
    if not event_token:
        return False
    return _seen_events.get(event_token, 0.0) > time.monotonic()


def _prune_expired(store: dict[str, float]) -> None:
    if len(store) > 1024:
        now = time.monotonic()
        for k in [k for k, v in store.items() if v <= now]:
            store.pop(k, None)


def _remember_event(event_token: str | None) -> None:
    """Помнит только УСПЕШНЫЕ вызовы: ретрай упавшего шага должен
    перерабатываться, а не глохнуть на «duplicate»."""
    if not event_token:
        return
    _prune_expired(_seen_events)
    _prune_expired(_selfcheck_ok)  # токены B24 ротируются — кэш не должен расти вечно
    _seen_events[event_token] = time.monotonic() + _EVENT_TTL


async def _resolve_host_addrs(host: str) -> list[str]:
    """Все адреса хоста (getaddrinfo живёт на event loop, не в модуле)."""
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, 443, type=socket.SOCK_STREAM
    )
    return [info[4][0] for info in infos]


async def _url_is_public_https(url: str) -> bool:
    """SSRF-фильтр [ссылки]: https, без userinfo/нестандартного порта, все
    DNS-адреса хоста публичны (режет loopback/private/link-local 169.254.169.254
    и IP-литералы в экзотической записи — их резолвит libc и отсекает is_global).
    Остаточный риск — DNS-rebinding TOCTOU (проверка и коннект резолвят хост
    раздельно); лечится пином IP в транспорте httpx, если понадобится."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return False
    try:
        port = parsed.port
    except ValueError:  # нечисловой/вне-диапазона порт в скобках URL
        return False
    if port not in (None, 443) or not parsed.hostname:
        return False
    try:
        addrs = await _resolve_host_addrs(parsed.hostname)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    for raw in addrs:
        try:
            ip = ipaddress.ip_address(raw.split("%")[0])  # v6 scope-id
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return True


def _split_message(text: str) -> tuple[str | None, str]:
    """(url первой [ссылки] | None, текст без неё; пробелы схлопнуты)."""
    m = _LINK_RE.search(text)
    if not m:
        return None, text
    cleaned = text[: m.start()] + text[m.end() :]
    return m.group(1), re.sub(r"[ \t]+", " ", cleaned).strip()


async def _download(url: str) -> tuple[StoredFile, str | None, str | None]:
    """Скачать файл в медиа-том (в память чанками — паритет upload-роута),
    вернуть (stored, mime, file_name). Ошибки — HTTPException с русским
    текстом (его увидит журнал БП через не-200)."""
    if not await _url_is_public_https(url):
        raise HTTPException(422, "Ссылка на файл должна быть публичным https-адресом")
    storage = get_media_storage()
    limit = storage.max_size_bytes
    file_name = sanitize_file_name(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1])
    chunks: list[bytes] = []
    size = 0
    try:
        async with httpx.AsyncClient(
            timeout=get_settings().bizproc_download_timeout_sec, follow_redirects=False
        ) as client, client.stream("GET", url) as resp:
            if resp.status_code >= 400:
                raise HTTPException(502, f"Не удалось скачать файл: HTTP {resp.status_code}")
            declared = resp.headers.get("Content-Length")
            if limit is not None and declared and declared.isdigit() and int(declared) > limit:
                raise HTTPException(413, "Файл больше допустимого размера")
            mime = normalize_mime(resp.headers.get("Content-Type"))
            if not mime_allowed_for_upload(mime):
                raise HTTPException(415, "Формат файла не поддерживается")
            async for chunk in resp.aiter_bytes(64 * 1024):
                size += len(chunk)
                if limit is not None and size > limit:
                    raise HTTPException(413, "Файл больше допустимого размера")
                chunks.append(chunk)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Не удалось скачать файл: {e.__class__.__name__}") from None
    stored = storage.save_bytes(b"".join(chunks), direction="out", ext=ext_for(file_name, mime))
    return stored, mime, file_name


async def _resolve_last_dialog(
    session: AsyncSession, entity_type: str, entity_id: int
) -> Dialog | None:
    """Последний диалог сущности (last_msg_at DESC; мультилинии → детерминированно
    самый свежий). Без фильтра status — паритет отправки из UI. deal терпит
    legacy-NULL типа (сделочной эры), у lead/contact строгие пары."""
    if entity_type == "contact":
        stmt = (
            select(Dialog)
            .join(Contact, Dialog.contact_id == Contact.id)
            .where(Contact.crm_contact_id == entity_id)
        )
    elif entity_type == "lead":
        stmt = select(Dialog).where(
            Dialog.crm_deal_id == entity_id, Dialog.crm_entity_type == "lead"
        )
    else:  # deal
        stmt = select(Dialog).where(
            Dialog.crm_deal_id == entity_id,
            or_(Dialog.crm_entity_type == "deal", Dialog.crm_entity_type.is_(None)),
        )
    stmt = stmt.order_by(Dialog.last_msg_at.desc().nullslast(), Dialog.id.desc()).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _is_initiation(session: AsyncSession, dialog_id: int) -> bool:
    exists = await session.execute(
        select(Message.id)
        .where(
            Message.dialog_id == dialog_id,
            Message.direction == MessageDirection.inbound,
        )
        .limit(1)
    )
    return exists.scalar_one_or_none() is None


async def handle_bizproc_message(
    body: bytes, content_type: str, secret_header: str | None, session: AsyncSession
) -> JSONResponse:
    """Ядро шага (выделено из роута для тестов без HTTP): парсинг → авторизация
    → дедуб → резолв диалога → [файл] → Message(+Attachment)+enqueue одним
    commit. Любая потеря до commit = не-200 (красный шаг в журнале БП)."""
    payload = _payload_dict(body, content_type)
    if payload is None:
        logger.warning("BIZPROC: payload отклонён (не JSON/form или >%d байт)", _MAX_BODY_BYTES)
        return JSONResponse({"error": "validation error"}, status_code=422)
    logger.info("BIZPROC payload: %s", _redacted(payload))

    ar = _activity_request(payload)
    if ar is None:
        logger.warning("BIZPROC: нет message/document_id или текст >%d", _MAX_TEXT_LEN)
        return JSONResponse({"error": "нет сообщения или документа"}, status_code=422)
    if not await _authorized(secret_header, ar, session):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if _is_recent_event(ar.event_token):
        logger.info("BIZPROC: дубль event_token, пропуск: entity=%s_%s", ar.entity_type, ar.entity_id)
        return JSONResponse({"status": "duplicate"})
    if ar.entity_type == "company":
        return JSONResponse(
            {"error": "Компании не поддерживаются: шаг работает со сделками, лидами и контактами"},
            status_code=409,
        )

    dialog = await _resolve_last_dialog(session, ar.entity_type, ar.entity_id)
    if dialog is None:
        return JSONResponse(
            {"error": f"У {_ENTITY_LABELS[ar.entity_type]} #{ar.entity_id} нет диалогов в ЧатМост"},
            status_code=409,
        )
    account = await session.get(TgAccount, dialog.account_id) if dialog.account_id else None
    if account is None or account.is_removed or account.status == TgAccountStatus.banned:
        return JSONResponse(
            {"error": "У линии диалога нет подключённого аккаунта"}, status_code=409
        )

    url, caption = _split_message(ar.text)
    if url is not None and len(caption) > _MAX_CAPTION_LEN:
        return JSONResponse(
            {"error": "Текст-подпись при файле — до 1024 символов"}, status_code=422
        )
    stored: StoredFile | None = None
    mime: str | None = None
    file_name: str | None = None
    if url is not None:
        stored, mime, file_name = await _download(url)

    # Постановка — зеркало send_media_message: плейсхолдер только в
    # Message.text (B24/превью), в очередь — реальный caption (может быть
    # пустым); файл на томе ДО строк БД.
    message_text = ar.text
    outbox_text = ar.text
    if stored is not None:
        message_text = caption or MEDIA_PLACEHOLDERS.get(attachment_type_for(mime), "[файл]")
        outbox_text = caption
    message = Message(
        dialog_id=dialog.id,
        direction=MessageDirection.outbound,
        text=message_text,
        status=MessageStatus.pending,
        author_user_id=ar.user_id,
    )
    session.add(message)
    attachment = None
    if stored is not None:
        attachment = Attachment(
            type=attachment_type_for(mime),
            file_path=stored.relative_path,
            mime_type=mime,
            size=stored.size,
            file_name=file_name,
        )
        # Append ДО flush: каскад поставит FK без lazy-load (MissingGreenlet).
        message.attachments.append(attachment)
    await session.flush()

    dialog.last_msg_at = message.created_at
    await SqlAlchemyOutboxRepository(session).enqueue(
        dialog_id=dialog.id,
        tg_account_id=account.id,
        external_chat_id=dialog.external_chat_id,
        text=outbox_text,
        is_initiation=await _is_initiation(session, dialog.id),
        message_id=message.id,
        attachment_id=attachment.id if attachment is not None else None,
    )
    await session.commit()
    _remember_event(ar.event_token)
    logger.info(
        "BIZPROC: сообщение в очередь: entity=%s_%s dialog=%s message=%s attach=%s",
        ar.entity_type,
        ar.entity_id,
        dialog.id,
        message.id,
        attachment is not None,
    )
    return JSONResponse({"status": "queued"})


@router.post("/bizproc")
async def bizproc_activity(request: Request, session: SessionDep) -> JSONResponse:
    """Вызов активити от очереди B24 (server-to-server, без verify_origin)."""
    return await handle_bizproc_message(
        await request.body(),
        request.headers.get("content-type", ""),
        request.headers.get("X-Webhook-Secret"),
        session,
    )
