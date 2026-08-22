"""Медиа WA: входящие — скачивание из OpenWA в MediaStorage.

Отправка отдельного клиента не требует: файл уже на общем томе, провайдер
кодирует его в base64 прямо в send-media (flat-DTO спеки 6.3; url-вариант
блокирует SSRF-guard сайдкара — внутренние адреса он не качает).
"""

import mimetypes
from pathlib import Path

from app.media.storage import MediaStorage
from app.messaging.types import MediaPayload
from app.messaging.whatsapp.api import OpenWaClient


class WaMedia:
    def __init__(self, *, api: OpenWaClient, storage: MediaStorage) -> None:
        self._api = api
        self._storage = storage

    async def download(
        self,
        *,
        session_id: str,
        chat_id: str,
        message_id: str,
        mimetype: str | None,
        file_name: str | None,
        direction: str = "in",
    ) -> MediaPayload:
        """direction: «in» — входящее, «out» — device-outbound владельца
        (контракт тома media: out/ — всё, что написал менеджер)."""
        data, ctype = await self._api.download_media(session_id, chat_id, message_id)
        mime = mimetype or ctype or "application/octet-stream"
        ext = Path(file_name).suffix if file_name else (mimetypes.guess_extension(mime) or "")
        stored = self._storage.save_bytes(data, direction=direction, ext=ext or None)
        return MediaPayload(
            path=stored.relative_path, mime_type=mime, size=len(data), file_name=file_name
        )
