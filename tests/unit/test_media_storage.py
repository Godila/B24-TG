"""Юнит-тесты MediaStorage: uuid-имена, лимит, path-guard, mime-правила."""

import pytest

from app.media.storage import (
    INLINE_MIME,
    MediaPathError,
    MediaStorage,
    MediaTooLargeError,
    attachment_type_for,
    ext_for,
    mime_allowed_for_upload,
    sanitize_file_name,
    serve_mime,
)
from app.models import AttachmentType


def test_new_path_creates_dir_and_returns_relative(tmp_path):
    store = MediaStorage(tmp_path / "media")
    absolute, relative = store.new_path(direction="in", ext="jpg")
    assert relative.startswith("in/")
    assert relative.endswith(".jpg")
    assert absolute.parent.is_dir()
    # Имя — uuid-hex: неугадываемо и без пользовательских частей.
    name = relative.rsplit("/", 1)[1]
    assert len(name) == 36  # 32 hex + ".jpg"
    assert name[:32].isalnum()


def test_new_path_rejects_bad_direction(tmp_path):
    store = MediaStorage(tmp_path)
    with pytest.raises(ValueError):
        store.new_path(direction="sideways")


def test_new_path_sanitizes_extension(tmp_path):
    store = MediaStorage(tmp_path)
    _, relative = store.new_path(direction="out", ext="../../etc/passwd")
    assert relative.startswith("out/")
    assert ".." not in relative


def test_save_bytes_roundtrip(tmp_path):
    store = MediaStorage(tmp_path, max_size_bytes=100)
    stored = store.save_bytes(b"hello", direction="out", ext="txt")
    assert stored.size == 5
    assert store.abs_path(stored.relative_path).read_bytes() == b"hello"


def test_save_bytes_enforces_limit(tmp_path):
    store = MediaStorage(tmp_path, max_size_bytes=4)
    with pytest.raises(MediaTooLargeError):
        store.save_bytes(b"toolong", direction="out")


def test_abs_path_ok(tmp_path):
    store = MediaStorage(tmp_path)
    p = store.abs_path("in/abc.jpg")
    assert p.is_absolute()
    assert str(tmp_path.resolve()) in str(p)


@pytest.mark.parametrize("rel", ["../secret.txt", "in/../../x"])
def test_abs_path_traversal_rejected(tmp_path, rel):
    store = MediaStorage(tmp_path)
    with pytest.raises(MediaPathError):
        store.abs_path(rel)


def test_is_writable(tmp_path):
    assert MediaStorage(tmp_path / "sub").is_writable() is True
    # Несоздаваемый родитель (файл вместо каталога) — не записываем.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    assert MediaStorage(blocker / "media").is_writable() is False


@pytest.mark.parametrize(
    ("mime", "allowed"),
    [
        ("image/png", True),
        ("image/jpeg", True),
        ("video/mp4", True),
        ("audio/ogg", True),
        ("application/pdf", True),
        ("application/zip", True),
        (None, False),
        ("", False),
        ("image/svg+xml", False),  # активный контент — режем явно
        ("text/html", False),
        ("application/x-msdownload", False),
        ("application/octet-stream", False),
    ],
)
def test_upload_allowlist(mime, allowed):
    assert mime_allowed_for_upload(mime) is allowed


def test_upload_allowlist_normalizes():
    assert mime_allowed_for_upload("IMAGE/PNG; charset=binary") is True


@pytest.mark.parametrize(
    ("mime", "expected"),
    [
        ("image/png", AttachmentType.photo),
        ("video/mp4", AttachmentType.video),
        ("audio/ogg", AttachmentType.voice),
        ("application/pdf", AttachmentType.file),
        (None, AttachmentType.file),
    ],
)
def test_attachment_type_for(mime, expected):
    assert attachment_type_for(mime) is expected


def test_serve_mime_inline_whitelist():
    assert serve_mime("image/jpeg") == ("image/jpeg", True)
    assert serve_mime("audio/ogg") == ("audio/ogg", True)
    # Всё вне списка — octet-stream + attachment (XSS-поверхность).
    assert serve_mime("application/pdf") == ("application/octet-stream", False)
    assert serve_mime("image/svg+xml") == ("application/octet-stream", False)
    assert serve_mime(None) == ("application/octet-stream", False)
    assert serve_mime("IMAGE/JPEG") == ("image/jpeg", True)  # нормализация


def test_inline_mime_set_has_no_svg_or_html():
    assert "image/svg+xml" not in INLINE_MIME
    assert "text/html" not in INLINE_MIME


def test_sanitize_file_name():
    assert sanitize_file_name("report.pdf") == "report.pdf"
    assert sanitize_file_name("C:\\Users\\x\\doc.pdf") == "doc.pdf"
    assert sanitize_file_name("/etc/passwd") == "passwd"
    assert sanitize_file_name("") is None
    assert sanitize_file_name(None) is None
    assert sanitize_file_name("x" * 500) == "x" * 200


def test_ext_for_prefers_name_then_mime():
    assert ext_for("photo.jpeg", "image/png") == "jpeg"
    assert ext_for(None, "image/png") == "png"
    assert ext_for("archive.", "application/zip") == "zip"
    assert ext_for("noext", None) is None
    # Странные расширения отбрасываются.
    assert ext_for("file.exe lol", None) is None
