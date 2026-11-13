"""Retention and deletion for files uploaded through ``POST /v1/upload_file``.

The security-relevant invariant of this module: a caller names a file only by its opaque
``file_id``. The storage URI that actually gets deleted is always read back from the
``uploaded_files`` row, never taken from the request, and is re-checked against the
caller's organization prefix by the storage layer before the object is removed.
"""

from __future__ import annotations

import asyncio
import os
import re
import zipfile
from datetime import datetime, timedelta, timezone

import filetype
import structlog
from fastapi import HTTPException, UploadFile

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.db.id import generate_uploaded_file_id
from skyvern.forge.sdk.schemas.files import UploadedFile

LOG = structlog.get_logger()

DEFAULT_TERMINAL_SIDE_EFFECT_TIMEOUT_SECONDS = 30.0


class InvalidRetentionPeriod(ValueError):
    pass


class FileNotAttachable(ValueError):
    """One or more file ids cannot be attached to the run."""

    def __init__(self, file_ids: list[str]) -> None:
        self.file_ids = file_ids
        super().__init__(
            "These file ids are not available to attach — each must name a file uploaded by this "
            f"organization that is not deleted and not already attached to another run: {', '.join(file_ids)}"
        )


class UploadStorageError(RuntimeError):
    """Raised when storage cannot persist an uploaded file."""


def generate_upload_id() -> str:
    """Mint a file id before the bytes are written, so it can be embedded in storage_uri.

    Two uploads of the same filename on the same day would otherwise compute the same
    storage key and silently overwrite each other's object; embedding the id keeps every
    row's URI unique so a delete can never dereference a URI another live row owns.
    """
    return generate_uploaded_file_id()


def resolve_expires_at(retention_days: int | None, *, now: datetime | None = None) -> datetime | None:
    """Turn a caller-supplied retention period into an absolute expiry.

    ``None`` means the caller asked for no expiry of its own — today's behavior, where the
    file lives until the organization's data-retention policy removes it.
    """
    if retention_days is None:
        return None
    if retention_days < 1 or retention_days > settings.MAX_UPLOADED_FILE_RETENTION_DAYS:
        raise InvalidRetentionPeriod(
            f"retention_days must be between 1 and {settings.MAX_UPLOADED_FILE_RETENTION_DAYS}"
        )
    return (now or datetime.now(timezone.utc)) + timedelta(days=retention_days)


async def record_upload(
    *,
    file_id: str,
    organization_id: str,
    storage_uri: str,
    filename: str,
    size_bytes: int | None,
    retention_days: int | None,
) -> UploadedFile:
    expires_at = resolve_expires_at(retention_days)
    uploaded_file = await app.DATABASE.uploaded_files.create_uploaded_file(
        file_id=file_id,
        organization_id=organization_id,
        storage_uri=storage_uri,
        filename=filename,
        size_bytes=size_bytes,
        expires_at=expires_at,
    )
    LOG.info(
        "Recorded uploaded file",
        organization_id=organization_id,
        file_id=uploaded_file.file_id,
        expires_at=expires_at,
        retention_days=retention_days,
    )
    return uploaded_file


async def validate_file_size(file: UploadFile, *, max_size_bytes: int | None = None) -> UploadFile:
    """Validate the ordinary upload byte limit before writing to storage."""
    try:
        file.file.seek(0, 2)
        size = file.file.tell()
        file.file.seek(0)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Could not determine file size.") from exc

    max_size = settings.MAX_UPLOAD_FILE_SIZE if max_size_bytes is None else max_size_bytes
    if size > max_size:
        raise HTTPException(
            status_code=413,
            detail=(f"File size exceeds the maximum allowed size ({max_size / 1024 / 1024} MB)"),
        )
    return file


_SNIFF_BYTES = 8192

# The expected upload types: each extension lists the MIME types a client may declare for it. Extensions
# outside a route's allowlist are refused before any content is read; signatures are checked below.
DOCUMENT_UPLOAD_TYPES: dict[str, tuple[str, ...]] = {
    "pdf": ("application/pdf", "application/x-pdf"),
    "doc": ("application/msword", "application/vnd.ms-word"),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",),
    "odt": ("application/vnd.oasis.opendocument.text",),
    "rtf": ("application/rtf", "application/x-rtf", "text/rtf"),
    "txt": ("text/plain",),
    "log": ("text/plain", "text/x-log"),
    "ics": ("text/calendar", "text/plain"),
    "vcf": ("text/vcard", "text/x-vcard", "text/directory", "text/plain"),
    "md": ("text/markdown", "text/x-markdown", "text/plain"),
    "eml": ("message/rfc822", "text/plain"),
    "msg": ("application/vnd.ms-outlook",),
}
SPREADSHEET_UPLOAD_TYPES: dict[str, tuple[str, ...]] = {
    "xls": ("application/vnd.ms-excel", "application/excel", "application/x-excel"),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",),
    "ods": ("application/vnd.oasis.opendocument.spreadsheet",),
    "csv": (
        "text/csv",
        "text/x-csv",
        "application/csv",
        "application/x-csv",
        "text/comma-separated-values",
        "application/vnd.ms-excel",
        "application/x-msexcel",
        "text/plain",
    ),
    "tsv": ("text/tab-separated-values", "text/tsv", "text/plain"),
}
PRESENTATION_UPLOAD_TYPES: dict[str, tuple[str, ...]] = {
    "ppt": ("application/vnd.ms-powerpoint",),
    "pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation",),
    "odp": ("application/vnd.oasis.opendocument.presentation",),
}
IMAGE_UPLOAD_TYPES: dict[str, tuple[str, ...]] = {
    "png": ("image/png", "image/x-png"),
    "jpg": ("image/jpeg", "image/jpg", "image/pjpeg"),
    "jpeg": ("image/jpeg", "image/jpg", "image/pjpeg"),
    "gif": ("image/gif",),
    "webp": ("image/webp",),
    "bmp": ("image/bmp", "image/x-ms-bmp", "image/x-bmp"),
    "tif": ("image/tiff", "image/x-tiff", "image/tif"),
    "tiff": ("image/tiff", "image/x-tiff", "image/tif"),
    "heic": ("image/heic", "image/heif", "image/heic-sequence"),
    "avif": ("image/avif",),
    "jfif": ("image/jpeg", "image/jpg", "image/pjpeg"),
}
DATA_UPLOAD_TYPES: dict[str, tuple[str, ...]] = {
    "json": ("application/json", "application/x-json", "text/json", "text/plain"),
    "xml": ("application/xml", "text/xml", "text/plain"),
    "yaml": ("application/yaml", "application/x-yaml", "text/yaml", "text/x-yaml", "text/plain"),
    "yml": ("application/yaml", "application/x-yaml", "text/yaml", "text/x-yaml", "text/plain"),
}
ARCHIVE_UPLOAD_TYPES: dict[str, tuple[str, ...]] = {
    "zip": ("application/zip", "application/x-zip-compressed", "application/x-zip", "multipart/x-zip"),
}
# Copilot chat attachments include screen recordings the model watches.
VIDEO_UPLOAD_TYPES: dict[str, tuple[str, ...]] = {
    "mp4": ("video/mp4",),
    "webm": ("video/webm",),
    "mov": ("video/quicktime", "video/x-quicktime"),
}
# Dictation is recorded in the browser and always named .webm, whatever container the browser produced.
AUDIO_UPLOAD_TYPES: dict[str, tuple[str, ...]] = {
    "webm": ("audio/webm", "video/webm"),
    "mp4": ("audio/mp4", "video/mp4"),
    "m4a": ("audio/mp4", "audio/x-m4a", "audio/m4a", "audio/mp4a-latm"),
    "mp3": ("audio/mpeg", "audio/mp3", "audio/x-mpeg"),
    "ogg": ("audio/ogg", "application/ogg", "video/ogg"),
    "oga": ("audio/ogg", "application/ogg", "video/ogg"),
    "wav": ("audio/wav", "audio/wave", "audio/x-wav", "audio/vnd.wave"),
    "flac": ("audio/flac", "audio/x-flac"),
}


def _merge_declared_types(*tables: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    merged: dict[str, tuple[str, ...]] = {}
    for table in tables:
        for extension, mime_types in table.items():
            merged[extension] = tuple(dict.fromkeys(merged.get(extension, ()) + mime_types))
    return merged


DECLARED_UPLOAD_TYPES = _merge_declared_types(
    DOCUMENT_UPLOAD_TYPES,
    SPREADSHEET_UPLOAD_TYPES,
    PRESENTATION_UPLOAD_TYPES,
    IMAGE_UPLOAD_TYPES,
    DATA_UPLOAD_TYPES,
    ARCHIVE_UPLOAD_TYPES,
    VIDEO_UPLOAD_TYPES,
    AUDIO_UPLOAD_TYPES,
)
# Per-route allowlists. The generic route takes everything a run may hand to a third-party form.
ALLOWED_UPLOAD_EXTENSIONS = frozenset(
    DOCUMENT_UPLOAD_TYPES
    | SPREADSHEET_UPLOAD_TYPES
    | PRESENTATION_UPLOAD_TYPES
    | IMAGE_UPLOAD_TYPES
    | DATA_UPLOAD_TYPES
    | ARCHIVE_UPLOAD_TYPES
    | VIDEO_UPLOAD_TYPES
    | AUDIO_UPLOAD_TYPES
)
PDF_UPLOAD_EXTENSIONS = frozenset({"pdf"})
# What the resume parser reads: PDF, DOCX and its image branch.
RESUME_UPLOAD_EXTENSIONS = frozenset({"pdf", "docx", "png", "jpg", "jpeg", "gif", "bmp", "webp", "tif", "tiff"})
VOICE_NOTE_UPLOAD_EXTENSIONS = frozenset(AUDIO_UPLOAD_TYPES)
# Clients that do not know a type send a generic one; anything more specific must agree with the extension.
_GENERIC_DECLARED_TYPES = frozenset({"", "application/octet-stream", "binary/octet-stream"})

# Second gate: anything the OS or a browser would execute, whatever the allowlist says.
DENIED_UPLOAD_EXTENSIONS = frozenset(
    {
        "app", "apk", "bat", "cmd", "com", "cpl", "deb", "dll", "dmg", "docm", "exe", "hta", "htm", "html", "jar",
        "js", "jse", "lnk", "msc", "mht", "mhtml", "msi", "php", "phtml", "pif", "pkg", "pptm", "ps1", "py", "reg",
        "rpm", "scf", "scr", "sh", "shtml", "svg", "svgz", "url", "vbe", "vbs", "wsf", "wsh", "xht", "xhtml", "xlsm",
    }
)  # fmt: skip

_DENIED_SNIFFED_MIME_TYPES = frozenset(
    {
        "application/x-msdownload",
        "application/x-executable",
        "application/x-deb",
        "application/x-rpm",
        "application/x-google-chrome-extension",
        "application/x-shockwave-flash",
    }
)
_MACHO_MAGICS = (
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
)
# Browsers render past leading comments, XML declarations and processing instructions.
_MARKUP_PREAMBLE_RE = re.compile(rb"^(?:\s+|<\?.*?\?>|<!--.*?-->)*", re.DOTALL)
# A preamble longer than the sniff window is read on up to this bound before the upload is refused.
_MARKUP_PREAMBLE_LIMIT = 1 << 20
_UNICODE_BOMS = (
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
    (b"\xef\xbb\xbf", "utf-8"),
)
_BROWSER_MARKUP_RE = re.compile(rb"^<(!doctype\s+(html|svg)|html|head|body|script|iframe|svg)[\s>/]", re.IGNORECASE)
_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06")
# Password-protected OOXML documents are stored as OLE compound files, not zips.
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_OLE_DIRECTORY_ENTRY_SIZE = 128
# Compound-file directory entries are 128-byte aligned: a NUL-terminated UTF-16 name, then its byte length at 64.
_OLE_VBA_PROJECT_ENTRY = "_VBA_PROJECT\0".encode("utf-16-le")
_OLE_ENCRYPTED_PACKAGE_ENTRY = "EncryptedPackage\0".encode("utf-16-le")
_OLE_SCAN_CHUNK = 1 << 20
_WINDOWS_SHORTCUT_MAGIC = b"L\x00\x00\x00\x01\x14\x02\x00\x00\x00\x00\x00\xc0\x00\x00\x00\x00\x00\x00F"
_OOXML_EXTENSIONS = frozenset({"docx", "xlsx", "pptx"})
_ODF_EXTENSIONS = frozenset({"odt", "ods", "odp"})
# filetype recognizes these reliably; HEIF brands and BigTIFF it does not.
_SNIFFED_IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "jfif", "gif", "webp"})
_TIFF_MAGICS = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
_MEDIA_CONTAINER_EXTENSIONS = frozenset({"mp4", "mov", "webm", "m4a"})
_EBML_MAGIC = b"\x1a\x45\xdf\xa3"
# ISO-BMFF files from older encoders may open with an atom other than ftyp.
_ISO_BMFF_LEAD_ATOMS = frozenset({b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot"})
_RTF_MAGIC = b"{\\rtf"
# An OpenDocument package keeps Basic macros under Basic/<library>/ and other script languages under Scripts/.
_ODF_MACRO_INDEX_FILES = frozenset({"script-lc.xml", "script-lb.xml", "dialog-lc.xml", "dialog-lb.xml"})


def upload_extension(filename: str | None) -> str:
    # Windows drops trailing dots and spaces, so "x.exe. " opens as an .exe.
    name = os.path.basename(filename or "").rstrip(" .")
    return os.path.splitext(name)[1].lstrip(".").lower()


def _is_pe(head: bytes) -> bool:
    if not head.startswith(b"MZ"):
        return False
    if len(head) >= 0x40:
        pe_offset = int.from_bytes(head[0x3C:0x40], "little")
        if head[pe_offset : pe_offset + 4] == b"PE\x00\x00":
            return True
    # A PE header past the sniff window still leaves NUL padding in the DOS stub; text never has NULs.
    return b"\x00" in head


def _markup_text(head: bytes) -> bytes:
    # Browsers honor a UTF-16 or UTF-32 BOM, so markup is matched after decoding it.
    for bom, encoding in _UNICODE_BOMS:
        if head.startswith(bom):
            return head[len(bom) :].decode(encoding, errors="ignore").encode("utf-8").lstrip()
    return head.lstrip()


def _markup_preamble_unfinished(head: bytes) -> bool:
    rest = _MARKUP_PREAMBLE_RE.sub(b"", _markup_text(head), count=1)
    # A tag cut at the window edge (``<scr``, ``<!-``) only matches once the rest of it is read.
    return not rest or rest.startswith((b"<!--", b"<?")) or (rest.startswith(b"<") and b">" not in rest)


def _dangerous_content_reason(head: bytes, extension: str) -> str | None:
    if _is_pe(head):
        return "Windows executable content"
    if head.startswith(b"\x7fELF") or head.startswith(_MACHO_MAGICS):
        return "executable content"
    if head.startswith(_WINDOWS_SHORTCUT_MAGIC):
        return "Windows shortcut"
    sniffed = filetype.guess(head)
    if sniffed is not None and sniffed.mime in _DENIED_SNIFFED_MIME_TYPES:
        return f"{sniffed.mime} content"
    text = _markup_text(head)
    if text.startswith(b"#!"):
        return "script content"
    if _BROWSER_MARKUP_RE.match(_MARKUP_PREAMBLE_RE.sub(b"", text, count=1)):
        return "HTML or SVG content"
    lowered = head.lower()
    # Saved emails share the MHTML layout but are not rendered as pages.
    if extension not in {"eml", "msg"} and b"mime-version:" in lowered and b"multipart/related" in lowered:
        return "MHTML content"
    return None


def _is_odf_macro_member(name: str) -> bool:
    if name.endswith("/"):
        return False
    if name.startswith("Scripts/"):
        return True
    # LibreOffice writes empty library indexes into documents that have no macros.
    return name.startswith("Basic/") and os.path.basename(name) not in _ODF_MACRO_INDEX_FILES


def _zip_member_reason(file: UploadFile, extension: str) -> str | None:
    # ZipFile reads only the central directory here, never member data.
    try:
        with zipfile.ZipFile(file.file) as archive:
            names = archive.namelist()
    except (zipfile.BadZipFile, OSError, ValueError):
        return f"content does not match .{extension}" if extension in _OOXML_EXTENSIONS | _ODF_EXTENSIONS else None
    finally:
        file.file.seek(0)
    # Every real OOXML package has a content-types part and every ODF package a manifest or mimetype entry.
    if extension in _OOXML_EXTENSIONS and "[Content_Types].xml" not in names:
        return f"content does not match .{extension}"
    if extension in _ODF_EXTENSIONS and not {"mimetype", "META-INF/manifest.xml"} & set(names):
        return f"content does not match .{extension}"
    if "[Content_Types].xml" in names and any(name.lower().endswith("vbaproject.bin") for name in names):
        return "Office document with macros"
    if {"mimetype", "META-INF/manifest.xml"} & set(names) and any(_is_odf_macro_member(n) for n in names):
        return "OpenDocument file with macros"
    if "AndroidManifest.xml" in names and "classes.dex" in names:
        return "Android package"
    if "META-INF/MANIFEST.MF" in names and any(name.endswith(".class") for name in names):
        return "Java archive"
    return None


def _ole_directory_entries(file: UploadFile, names: tuple[bytes, ...]) -> set[bytes]:
    found: set[bytes] = set()
    offset = 0
    try:
        file.file.seek(0)
        while chunk := file.file.read(_OLE_SCAN_CHUNK):
            for name in names:
                start = 0
                while (index := chunk.find(name, start)) != -1:
                    aligned = (offset + index) % _OLE_DIRECTORY_ENTRY_SIZE == 0
                    if aligned and chunk[index + 64 : index + 66] == len(name).to_bytes(2, "little"):
                        found.add(name)
                    start = index + 1
            offset += len(chunk)
    finally:
        file.file.seek(0)
    return found


def _ole_reason(file: UploadFile, extension: str) -> str | None:
    # Legacy .doc/.xls keep VBA in a _VBA_PROJECT stream; an OLE file named as OOXML must be an encrypted package.
    entries = _ole_directory_entries(file, (_OLE_VBA_PROJECT_ENTRY, _OLE_ENCRYPTED_PACKAGE_ENTRY))
    if _OLE_VBA_PROJECT_ENTRY in entries:
        return "Office document with macros"
    if extension in _OOXML_EXTENSIONS and _OLE_ENCRYPTED_PACKAGE_ENTRY not in entries:
        return f"content does not match .{extension}"
    return None


def _is_media_container(head: bytes) -> bool:
    return head.startswith(_EBML_MAGIC) or head[4:8] in _ISO_BMFF_LEAD_ATOMS


def has_audio_container_signature(head: bytes) -> bool:
    return (
        _is_media_container(head)
        or head.startswith((b"OggS", b"ID3", b"fLaC"))
        or (head[:4] == b"RIFF" and head[8:12] == b"WAVE")
        or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)
    )


def _declared_type_mismatch(head: bytes, extension: str, any_audio_container: bool) -> bool:
    """True when the content contradicts a format whose signature is reliable.

    Formats with common unsigned variants (legacy .doc/.xls/.ppt saved as text, XML or a renamed zip) have no
    positive check; the denylist, markup sniff and macro scans still apply to them.
    """
    if extension == "pdf":
        # Readers accept a PDF header anywhere in the first KiB.
        return b"%PDF-" not in head[:1024]
    if extension == "zip" or extension in _ODF_EXTENSIONS:
        return not head.startswith(_ZIP_MAGICS)
    if extension in _OOXML_EXTENSIONS:
        return not head.startswith(_ZIP_MAGICS + (_OLE_MAGIC,))
    if extension == "rtf":
        return not head.removeprefix(b"\xef\xbb\xbf").lstrip().startswith(_RTF_MAGIC)
    if extension in _SNIFFED_IMAGE_EXTENSIONS:
        return not filetype.is_image(head)
    if extension == "bmp":
        return not head.startswith(b"BM")
    if extension in {"tif", "tiff"}:
        return not head.startswith(_TIFF_MAGICS)
    if extension in {"heic", "avif"}:
        return head[4:8] != b"ftyp"
    if any_audio_container:
        # Dictation is named .webm whatever container the browser recorded into.
        return not has_audio_container_signature(head)
    if extension in _MEDIA_CONTAINER_EXTENSIONS:
        return not _is_media_container(head)
    return False


def _declared_mime_type(file: UploadFile) -> str:
    return (file.content_type or "").split(";", 1)[0].strip().lower()


def _sniff_head(file: UploadFile) -> bytes:
    try:
        head = file.file.read(_SNIFF_BYTES)
        if len(head) == _SNIFF_BYTES and _markup_preamble_unfinished(head):
            head += file.file.read(_MARKUP_PREAMBLE_LIMIT - _SNIFF_BYTES)
    finally:
        file.file.seek(0)
    return head


def _operator_extra_extensions() -> frozenset[str]:
    extra = (extension.strip().lstrip(".").lower() for extension in settings.UPLOAD_EXTRA_ALLOWED_EXTENSIONS)
    return frozenset(extension for extension in extra if extension)


def _upload_type_rejection_reason(
    file: UploadFile,
    allowed: frozenset[str],
    *,
    enforce_allowlist: bool = True,
    any_declared_type: frozenset[str] = frozenset(),
    any_audio_container: bool = False,
) -> str | None:
    # Gate 1: the allowlist, by extension and declared MIME type. No bytes are read for a refused extension.
    # Pass-through files skip it, because a third-party site may ask for any type.
    extension = upload_extension(file.filename)
    if enforce_allowlist and extension and extension not in allowed:
        return f".{extension} files are not allowed"
    head = _sniff_head(file)
    if enforce_allowlist:
        if not extension:
            sniffed = filetype.guess(head)
            extension = sniffed.extension if sniffed is not None else ""
            if extension not in allowed:
                return "no file extension and the content is not a recognized type"
        declared = _declared_mime_type(file)
        # An operator-listed extension takes any declared type, so the setting can also reopen an alias refusal.
        expected_types = None if extension in any_declared_type else DECLARED_UPLOAD_TYPES.get(extension)
        if declared not in _GENERIC_DECLARED_TYPES and expected_types is not None and declared not in expected_types:
            return f"declared type {declared} does not match .{extension}"
    # Gate 2: the denylist and content sniffing, for renamed executables, markup and macro documents.
    if extension in DENIED_UPLOAD_EXTENSIONS:
        return f".{extension} files are not allowed"
    if len(head) == _MARKUP_PREAMBLE_LIMIT and _markup_preamble_unfinished(head):
        return "markup preamble too long to inspect"
    if reason := _dangerous_content_reason(head, extension):
        return reason
    if _declared_type_mismatch(head, extension, any_audio_container):
        return f"content does not match .{extension}"
    if head.startswith(_ZIP_MAGICS):
        return _zip_member_reason(file, extension)
    if head.startswith(_OLE_MAGIC):
        return _ole_reason(file, extension)
    return None


def validate_upload_type(
    file: UploadFile,
    *,
    purpose: str,
    organization_id: str | None = None,
    allowed: frozenset[str] | None = None,
    any_audio_container: bool = False,
) -> UploadFile:
    """Allow only the route's expected types, then refuse executable, scripted, renderable or mislabeled content.

    The allowlist is by extension and declared MIME type (a file without an extension must sniff as an allowed
    type); the denylist and content sniffing stay as the second gate for renamed executables and macro documents.
    ``allowed`` defaults to the generic route's set, the only one ``UPLOAD_EXTRA_ALLOWED_EXTENSIONS`` widens.
    """
    operator_extra = _operator_extra_extensions() if allowed is None else frozenset()
    reason = _upload_type_rejection_reason(
        file,
        ALLOWED_UPLOAD_EXTENSIONS | operator_extra if allowed is None else allowed,
        any_declared_type=operator_extra,
        any_audio_container=any_audio_container,
    )
    if reason is not None:
        LOG.info(
            "Rejected upload by file type",
            purpose=purpose,
            organization_id=organization_id,
            extension=upload_extension(file.filename),
            declared_type=_declared_mime_type(file),
            reason=reason,
        )
        raise HTTPException(status_code=415, detail=f"File type not allowed: {reason}")
    return file


async def validate_upload(
    file: UploadFile,
    *,
    purpose: str,
    organization_id: str | None = None,
    max_size_bytes: int | None = None,
    allowed: frozenset[str] | None = None,
) -> UploadFile:
    await validate_file_size(file, max_size_bytes=max_size_bytes)
    # The OLE macro scan reads the whole file, so it runs off the event loop.
    return await asyncio.to_thread(
        validate_upload_type, file, purpose=purpose, organization_id=organization_id, allowed=allowed
    )


async def validate_passthrough_upload(file: UploadFile, *, max_size_bytes: int | None, purpose: str) -> UploadFile:
    """Size-check a file the agent hands to a third-party site, which may legitimately ask for any type.

    The type rules are logged rather than enforced; the stored copy is only ever served as an attachment.
    """
    await validate_file_size(file, max_size_bytes=max_size_bytes)
    reason = await asyncio.to_thread(_upload_type_rejection_reason, file, frozenset(), enforce_allowlist=False)
    if reason is not None:
        LOG.info(
            "Accepted a restricted file type for pass-through",
            extension=upload_extension(file.filename),
            reason=reason,
            purpose=purpose,
        )
    return file


async def save_uploaded_file(
    *,
    file: UploadFile,
    organization_id: str,
    retention_days: int | None = None,
) -> tuple[UploadedFile, str]:
    """Persist bytes and the ordinary uploaded-file record.

    The returned URL is only for the ordinary upload response. Callers that launch a run must
    use the returned record's durable ``storage_uri`` instead.
    """
    resolve_expires_at(retention_days)
    file_id = generate_upload_id()
    storage_filename = f"{file_id}_{os.path.basename(file.filename)}" if file.filename else file_id
    uris = await app.STORAGE.save_legacy_file(
        organization_id=organization_id,
        filename=storage_filename,
        fileObj=file.file,
    )
    if not uris:
        raise UploadStorageError("Failed to upload file to S3.")
    presigned_url, uploaded_s3_uri = uris
    uploaded_file = await record_upload(
        file_id=file_id,
        organization_id=organization_id,
        storage_uri=uploaded_s3_uri,
        filename=file.filename or "",
        size_bytes=file.size,
        retention_days=retention_days,
    )
    return uploaded_file, presigned_url


async def delete_uploaded_file(*, file_id: str, organization_id: str) -> bool:
    """Delete an uploaded file's bytes and retire its row. False when there is no such live file.

    The object is removed before the row is retired, so a storage failure surfaces as an
    error against a file that is still listed rather than as a row the caller believes is
    deleted while the bytes remain. The caller can retry; deleting an already-absent object
    is a no-op, so the retry converges.
    """
    uploaded_file = await app.DATABASE.uploaded_files.get_uploaded_file(
        file_id=file_id, organization_id=organization_id
    )
    if uploaded_file is None:
        return False

    await _delete_stored_object(uploaded_file)
    await app.DATABASE.uploaded_files.claim_uploaded_file_for_deletion(file_id=file_id, organization_id=organization_id)
    LOG.info(
        "Deleted uploaded file",
        organization_id=organization_id,
        file_id=uploaded_file.file_id,
    )
    return True


async def assert_files_attachable(*, file_ids: list[str], organization_id: str) -> None:
    """Reject unknown, deleted, or already-attached file ids before a run is created.

    Attaching happens after the run exists, which is too late to answer 4xx. Checking first
    means a bad file id costs the caller nothing instead of leaving a started run holding an
    attachment it never got.
    """
    requested = list(dict.fromkeys(file_ids))
    if not requested:
        return
    live = await app.DATABASE.uploaded_files.get_uploaded_files_by_ids(
        file_ids=requested, organization_id=organization_id
    )
    attachable = {uploaded_file.file_id for uploaded_file in live if uploaded_file.run_id is None}
    unattachable = [file_id for file_id in requested if file_id not in attachable]
    if unattachable:
        raise FileNotAttachable(unattachable)


async def live_uploaded_files(*, file_ids: list[str], organization_id: str) -> dict[str, UploadedFile]:
    """The live files this organization uploaded among ``file_ids``, by id."""
    requested = list(dict.fromkeys(file_ids))
    if not requested:
        return {}
    live = await app.DATABASE.uploaded_files.get_uploaded_files_by_ids(
        file_ids=requested, organization_id=organization_id
    )
    return {uploaded_file.file_id: uploaded_file for uploaded_file in live if not is_past_retention(uploaded_file)}


async def attach_files_to_run(*, file_ids: list[str], organization_id: str, run_id: str) -> list[UploadedFile]:
    """Bind uploaded files to a run so the run's terminal handler deletes them.

    Every attachment also gets a backstop expiry: the terminal handler is the fast path, and
    the existing expiry sweep is what guarantees the bytes go away even if a run never reaches
    it. Without the backstop, a lost worker would strand exactly the sensitive data this
    feature exists to bound.
    """
    if not file_ids:
        return []

    backstop = datetime.now(timezone.utc) + timedelta(hours=settings.RUN_ATTACHED_FILE_BACKSTOP_HOURS)
    attached = await app.DATABASE.uploaded_files.attach_uploaded_files_to_run(
        file_ids=list(dict.fromkeys(file_ids)),
        organization_id=organization_id,
        run_id=run_id,
        expires_at=backstop,
    )
    attached_ids = {uploaded_file.file_id for uploaded_file in attached}
    missed = [file_id for file_id in dict.fromkeys(file_ids) if file_id not in attached_ids]
    if missed:
        # Lost a race with a concurrent delete or another run's attach between the pre-check
        # and here. The run is already created, so this is logged rather than raised: the run
        # proceeds and simply has no attachment to delete for those ids.
        LOG.warning(
            "Some files could not be attached to the run",
            organization_id=organization_id,
            run_id=run_id,
            file_ids=missed,
        )
    if attached:
        LOG.info(
            "Attached uploaded files to run",
            organization_id=organization_id,
            run_id=run_id,
            file_ids=sorted(attached_ids),
            backstop_expires_at=backstop,
        )
    return attached


async def delete_files_attached_to_run(
    *,
    run_id: str,
    timeout_seconds: float | None = None,
) -> int:
    try:
        async with asyncio.timeout(timeout_seconds):
            return await _delete_files_attached_to_run(run_id=run_id)
    except TimeoutError:
        LOG.warning(
            "Timed out while deleting files attached to run",
            run_id=run_id,
            timeout_seconds=timeout_seconds,
        )
        return 0
    except Exception:
        LOG.exception("Failed to delete files attached to run", run_id=run_id)
        return 0


async def _delete_files_attached_to_run(*, run_id: str) -> int:
    """Delete every file attached to a run. Safe to call for runs that have no attachments.

    Never raises. This runs inside run teardown, where an exception would cost the run its
    webhook and artifact persistence — a far worse outcome than a late delete. Anything that
    fails here is picked up by the expiry sweep via the backstop expiry set at attach time.
    """
    try:
        attached = await app.DATABASE.uploaded_files.get_uploaded_files_for_run(run_id=run_id)
    except Exception:
        LOG.exception("Failed to look up files attached to run", run_id=run_id)
        return 0
    if not attached:
        return 0

    deleted = 0
    for uploaded_file in attached:
        try:
            await _delete_stored_object(uploaded_file)
        except Exception:
            LOG.exception(
                "Failed to delete file attached to run",
                organization_id=uploaded_file.organization_id,
                run_id=run_id,
                file_id=uploaded_file.file_id,
            )
            continue
        try:
            await app.DATABASE.uploaded_files.claim_uploaded_file_for_deletion(
                file_id=uploaded_file.file_id, organization_id=uploaded_file.organization_id
            )
        except Exception:
            LOG.exception(
                "Deleted the bytes of a run attachment but failed to retire its row",
                organization_id=uploaded_file.organization_id,
                run_id=run_id,
                file_id=uploaded_file.file_id,
            )
            continue
        deleted += 1

    LOG.info(
        "Deleted files attached to run",
        run_id=run_id,
        attached=len(attached),
        deleted=deleted,
    )
    return deleted


async def file_id_for_storage_uri(*, storage_uri: str, organization_id: str) -> str | None:
    uploaded_file = await app.DATABASE.uploaded_files.get_uploaded_file_by_storage_uri(
        storage_uri=storage_uri, organization_id=organization_id
    )
    return uploaded_file.file_id if uploaded_file else None


async def resolve_file_reference(*, file_id: str, organization_id: str) -> str | None:
    """Return the storage URI behind a file id, or None when the org has no such live file.

    A row past its ``expires_at`` reports as missing rather than handing back a URI whose object the
    next purge deletes.

    This is what lets a caller hand the agent a file id instead of a presigned URL: the URI is
    read from the row rather than taken from input, and the storage layer re-checks it against
    the organization's prefix before any bytes are read.
    """
    uploaded_file = await app.DATABASE.uploaded_files.get_uploaded_file(
        file_id=file_id, organization_id=organization_id
    )
    if uploaded_file is None or is_past_retention(uploaded_file):
        return None
    return uploaded_file.storage_uri


# The only upload types a block image may name: raster images render inline and run no script.
BLOCK_IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
# The uploads bucket moves objects over 4 MiB to Deep Archive after 92 days, where they cannot be read.
BLOCK_IMAGE_MAX_BYTES = 4 * 1024 * 1024


def is_block_image_upload(uploaded_file: UploadedFile) -> bool:
    # The row records no content type, so the extension decides; the upload routes accept a renamed BMP, TIFF or ICO
    # under a .png name, and the signed-URL route, which sniffs the stored bytes, is the real gate.
    media_types = IMAGE_UPLOAD_TYPES.get(upload_extension(uploaded_file.filename), ())
    return bool(media_types) and media_types[0] in BLOCK_IMAGE_MEDIA_TYPES


def is_past_retention(uploaded_file: UploadedFile) -> bool:
    # The purge runs hourly, so an expired row can still name live bytes. The retention the caller
    # was promised governs the dereference, not when the sweep happens to run.
    expires_at = uploaded_file.expires_at
    if expires_at is None:
        return False
    expiry = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
    return expiry <= datetime.now(timezone.utc)


async def purge_expired_files(*, limit: int = 500) -> dict[str, int]:
    """Delete files whose retention period has elapsed.

    Only rows that carry an ``expires_at`` are eligible — an expiry the caller asked for at
    upload, or one set by the organization's data-retention policy. Deleting an object that
    is already gone is a no-op in every storage backend, so a row whose object went first
    still converges.
    """
    now = datetime.now(timezone.utc)
    expired = await app.DATABASE.uploaded_files.get_expired_uploaded_files(before=now, limit=limit)
    if not expired:
        return {"examined": 0, "deleted": 0, "failed": 0}

    deleted = 0
    failed = 0
    for candidate in expired:
        try:
            # Same ordering as the API delete: a row left live after a storage failure is
            # still expired, so the next tick retries it. Retiring it first would strand
            # the object with nothing left to find it.
            await _delete_stored_object(candidate)
        except Exception:
            failed += 1
            LOG.exception(
                "Failed to delete expired uploaded file",
                organization_id=candidate.organization_id,
                file_id=candidate.file_id,
            )
            continue
        await app.DATABASE.uploaded_files.claim_uploaded_file_for_deletion(
            file_id=candidate.file_id, organization_id=candidate.organization_id
        )
        deleted += 1

    LOG.info("Purged expired uploaded files", examined=len(expired), deleted=deleted, failed=failed)
    return {"examined": len(expired), "deleted": deleted, "failed": failed}


async def _delete_stored_object(uploaded_file: UploadedFile) -> None:
    await app.STORAGE.delete_legacy_file(
        organization_id=uploaded_file.organization_id,
        uri=uploaded_file.storage_uri,
    )
