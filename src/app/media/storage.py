"""Хранилище медиа-вложений на общем docker-томе (web + bridge).

Единая точка правил для двух писателей (web: upload менеджера; bridge:
download из канала) и одного читателя (авторизованная раздача):

- имена на диске — только серверные ``uuid4().hex + ext`` (path traversal
  невозможен конструктивно); ``abs_path`` дополнительно резолвит путь и
  проверяет принадлежность корню — защита от протухших/подменённых
  значений ``attachments.file_path`` в БД;
- подпапки ``in/`` и ``out/`` разделяют источник файла (аудит, чистка
  файлов-сирот): ``in/`` — скачанное из канала, ``out/`` — загруженное
  менеджером И медиа его device-outbound сообщений (скачанное из канала,
  но семантически исходящее);
- один лимит размера на вход и выход, = nginx client_max_body_size.

Методы синхронные сознательно: локальный docker-том, файлы ≤25 МБ —
``asyncio.to_thread`` здесь преждевременная оптимизация.
"""

from __future__ import annotations

import mimetypes
import re
import shutil
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.models import AttachmentType

#: MIME, которые можно отдавать inline (рендерятся в пузыре чата).
#: Всё остальное — octet-stream + Content-Disposition: attachment:
#: браузер не должен интерпретировать произвольный документ на нашем
#: домене (XSS-поверхность), nosniff от nginx дополняет защиту.
INLINE_MIME: frozenset[str] = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "video/mp4",
        "video/webm",
        "audio/ogg",
        "audio/mpeg",
        "audio/mp4",
        "audio/wav",
    }
)

#: Отвергаются на загрузке до записи на диск. SVG — активный контент
#: (исполняет скрипт в <img>-контексте некоторых браузеров и при inline
#: раздаче) — в white-list ниже ``image/`` не спасает, режем явно.
REJECTED_MIME: frozenset[str] = frozenset({"image/svg+xml", "text/html", "application/xhtml+xml"})

#: Разрешённые к ЗАГРУЗКЕ типы: медиа по префиксу + «канцелярские»
#: документы точно (Wazzup-подобный allowlist; входящие из TG не
#: ограничиваются списком — их присылает клиент, мы только сохраняем).
_UPLOAD_MIME_PREFIXES = ("image/", "video/", "audio/")
_UPLOAD_MIME_EXACT = frozenset(
    {
        "application/pdf",
        "application/zip",
        "application/x-zip-compressed",
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
)

_SAFE_EXT_RE = re.compile(r"^[a-z0-9]{1,8}$")


class MediaTooLargeError(Exception):
    """Файл больше настроенного лимита (HTTP 413)."""


class MediaPathError(Exception):
    """Путь выходит за пределы медиа-тома (битые данные в БД)."""


@dataclass(frozen=True, slots=True)
class StoredFile:
    """Результат сохранения файла: относительный путь + фактический размер."""

    relative_path: str
    size: int


def normalize_mime(mime: str | None) -> str | None:
    """MIME для хранения/сравнения: без параметров, lower, ≤128 (колонка БД).

    Сырой Content-Type контролирует отправитель (HTTP-заголовок, входящих
    TG-документов — клиент отправителя): без обрезки длинное значение роняет
    INSERT на Postgres (StringDataRightTruncation) уже ПОСЛЕ записи файла
    на диск — повторяемая утечка сирот.
    """
    if not mime:
        return None
    return mime.split(";")[0].strip().lower()[:128] or None


def mime_allowed_for_upload(mime: str | None) -> bool:
    """Разрешён ли тип к загрузке менеджером (исходящие)."""
    m = normalize_mime(mime)
    if m is None or m in REJECTED_MIME:
        return False
    return m.startswith(_UPLOAD_MIME_PREFIXES) or m in _UPLOAD_MIME_EXACT


def attachment_type_for(mime: str | None) -> AttachmentType:
    """Классификация вложения по MIME (виден в UI как что)."""
    m = normalize_mime(mime)
    if m is None:
        return AttachmentType.file
    if m.startswith("image/"):
        return AttachmentType.photo
    if m.startswith("video/"):
        return AttachmentType.video
    if m.startswith("audio/"):
        return AttachmentType.voice
    return AttachmentType.file


def serve_mime(mime: str | None) -> tuple[str, bool]:
    """(Content-Type, inline) для раздачи: только безопасный inline-список."""
    m = normalize_mime(mime)
    if m in INLINE_MIME:
        return m, True
    return "application/octet-stream", False


def sanitize_file_name(name: str | None) -> str | None:
    """Имя файла для метаданных: basename, без control-символов, ≤200."""
    if not name:
        return None
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    return name[:200] or None


def ext_for(file_name: str | None, mime: str | None) -> str | None:
    """Расширение из оригинального имени, иначе из MIME; None — без ext."""
    if file_name and "." in file_name:
        ext = file_name.rsplit(".", 1)[1].lower()
        if _SAFE_EXT_RE.match(ext):
            return ext
    m = normalize_mime(mime)
    if m:
        guessed = mimetypes.guess_extension(m)
        if guessed:
            ext = guessed.lstrip(".").lower()
            if _SAFE_EXT_RE.match(ext):
                return ext
    return None


class MediaStorage:
    """Файловое хранилище вложений: uuid-имена, лимит, path-guard."""

    def __init__(self, root: str | Path, *, max_size_bytes: int | None = None):
        self._root = Path(root)
        self._max_size_bytes = max_size_bytes

    @property
    def root(self) -> Path:
        return self._root

    @property
    def max_size_bytes(self) -> int | None:
        return self._max_size_bytes

    def is_writable(self) -> bool:
        """Проба записи (для /health и старта bridge); не бросает."""
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            probe = self._root / ".probe"
            probe.write_bytes(b"")
            probe.unlink()
            return True
        except OSError:
            return False

    def free_bytes(self) -> int | None:
        """Свободное место на томе (None — не удалось узнать)."""
        try:
            return shutil.disk_usage(self._root).free
        except OSError:
            return None

    def new_path(self, *, direction: str, ext: str | None = None) -> tuple[Path, str]:
        """Свежий (абсолютный, относительный) путь для писателя файла.

        ``direction``: ``in`` (скачано из канала) | ``out`` (исходящее: upload
        менеджера или медиа его device-outbound сообщения). Папка
        создаётся; имя неугадываемо (uuid4).
        """
        if direction not in ("in", "out"):
            raise ValueError(f"direction must be 'in' or 'out', got {direction!r}")
        safe_ext = ""
        if ext:
            ext = ext.lstrip(".").lower()
            if _SAFE_EXT_RE.match(ext):
                safe_ext = f".{ext}"
        relative = f"{direction}/{uuid.uuid4().hex}{safe_ext}"
        absolute = self.abs_path(relative)
        absolute.parent.mkdir(parents=True, exist_ok=True)
        return absolute, relative

    def save_bytes(self, data: bytes, *, direction: str, ext: str | None = None) -> StoredFile:
        """Сохранить байты (upload менеджера). Лимит → MediaTooLargeError."""
        if self._max_size_bytes is not None and len(data) > self._max_size_bytes:
            raise MediaTooLargeError(f"file size {len(data)} exceeds limit {self._max_size_bytes}")
        absolute, relative = self.new_path(direction=direction, ext=ext)
        absolute.write_bytes(data)
        return StoredFile(relative_path=relative, size=len(data))

    def abs_path(self, relative: str) -> Path:
        """Абсолютный путь внутри тома; выход за корень → MediaPathError."""
        resolved = (self._root / relative).resolve()
        if not resolved.is_relative_to(self._root.resolve()):
            raise MediaPathError(f"path escapes media root: {relative!r}")
        return resolved


@lru_cache(maxsize=1)
def get_media_storage() -> MediaStorage:
    """Синглтон для web-процесса (роуты/health); bridge строит свой в main."""
    from app.config import get_settings

    settings = get_settings()
    return MediaStorage(settings.media_dir, max_size_bytes=settings.media_max_size_bytes)
