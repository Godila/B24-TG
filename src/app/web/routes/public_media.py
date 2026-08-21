"""Публичная раздача медиа по подписи (imconnector: B24 качает files[].url).

В отличие от /api/attachments/…/file — без сессии и verify_origin: URL
подписан HMAC и ограничен TTL (см. app/media/public_url.py). Ошибки —
единый 404 (не раскрываем существование файла).
"""

import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.media.public_url import verify_media_sig
from app.media.storage import MediaPathError, get_media_storage, serve_mime
from app.models import Attachment

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/media/public", tags=["media"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/{attachment_id}/{exp}/{sig}")
async def public_attachment(
    attachment_id: int, exp: int, sig: str, session: SessionDep
) -> FileResponse:
    if not verify_media_sig(
        attachment_id, exp, sig, secret=get_settings().session_secret
    ):
        raise HTTPException(status_code=404)
    attachment = (
        await session.execute(select(Attachment).where(Attachment.id == attachment_id))
    ).scalar_one_or_none()
    if attachment is None:
        raise HTTPException(status_code=404)
    try:
        path = get_media_storage().abs_path(attachment.file_path)
    except MediaPathError:
        logger.warning("public media: broken file_path=%r", attachment.file_path)
        raise HTTPException(status_code=404) from None
    if not path.is_file():
        logger.warning("public media: file missing: %s", attachment.file_path)
        raise HTTPException(status_code=404)
    media_type, inline = serve_mime(attachment.mime_type)
    return FileResponse(
        path,
        media_type=media_type,
        filename=attachment.file_name or path.name,
        content_disposition_type="inline" if inline else "attachment",
        # Кэш — не дольше остатка жизни подписи: shared-кэш не должен
        # раздавать файл после её истечения.
        headers={"Cache-Control": f"public, max-age={max(0, exp - int(time.time()))}"},
    )
