#!/usr/bin/env python3
"""Validate the public blog source with canonical CommonMark tokenization."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import stat
import struct
import subprocess
import sys
import tarfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Sequence
from urllib.parse import unquote, unquote_to_bytes, urlsplit

from markdown_it import MarkdownIt
from mdit_py_plugins.footnote import footnote_plugin

MAX_POST_BYTES = 524_288
MAX_MEDIA_FILES = 20
MAX_MEDIA_FILE_BYTES = 5_242_880
MAX_MEDIA_DIRECTORY_BYTES = 26_214_400
MAX_STATIC_AXIS = 8_192
MAX_STATIC_PIXELS = 32_000_000
MAX_GIF_AXIS = 4_096
MAX_GIF_FRAMES = 100
MAX_GIF_FRAME_PIXELS = 16_000_000
MAX_GIF_SUMMED_PIXELS = 80_000_000

BOOTSTRAP_POST_COUNT = 277
BOOTSTRAP_SLUG_SET_SHA256 = "93d8eb6b42f24b1ff65d245aa3e2767f36e616aa98c397255908f176281f2e65"
BOOTSTRAP_STATE_COUNTS = Counter({"published": 275, "sent": 1, "draft": 1})
CONTRACT_PATH = "scripts/blog_content_contract.json"
IMMUTABLE_GUARD_PATHS = (
    ".github/sync.yml",
    ".github/workflows/ci.yml",
    "scripts/validate_blog_content.py",
    "scripts/tests/test_validate_blog_content.py",
)
APPROVED_SYNC_MAPPINGS = (
    ("skyvern/", "skyvern/", True),
    ("pyproject.toml", "pyproject.toml", False),
    ("uv.lock", "uv.lock", False),
    ("setup.sh", "setup.sh", False),
    (".env.example", ".env.example", False),
    (".nvmrc", ".nvmrc", False),
    ("run_ui.sh", "run_ui.sh", False),
    ("run_alembic_check.sh", "run_alembic_check.sh", False),
    ("skyvern-frontend/src/", "skyvern-frontend/src/", True),
    ("skyvern-frontend/tailwind.config.js", "skyvern-frontend/tailwind.config.js", False),
    ("evaluation/", "evaluation/", True),
    ("fern/", "fern/", True),
    ("docs/", "docs/", True),
    ("blogs/", "blogs/", True),
    ("tests/__init__.py", "tests/__init__.py", False),
    ("tests/conftest.py", "tests/conftest.py", False),
    ("tests/test_agent.py", "tests/test_agent.py", False),
    ("tests/unit/", "tests/unit/", True),
    ("tests/unit_tests/", "tests/unit_tests/", True),
    ("tests/smoke_tests/", "tests/smoke_tests/", True),
)

REQUIRED_KEYS = (
    "title",
    "description",
    "excerpt",
    "slug",
    "publicationState",
    "publishedAt",
    "updatedAt",
    "author",
    "tags",
    "featureImage",
    "featureImageAlt",
    "featureImageCaption",
    "sendNewsletter",
    "migratedFromGhost",
)
OPTIONAL_KEYS = (
    "seoTitle",
    "ogTitle",
    "ogDescription",
    "ogImage",
    "twitterTitle",
    "twitterDescription",
    "twitterImage",
    "twitterCard",
    "twitterUrl",
    "twitterSite",
    "twitterLabel1",
    "twitterData1",
    "twitterLabel2",
    "twitterData2",
)
ALLOWED_KEYS = frozenset(REQUIRED_KEYS + OPTIONAL_KEYS)
STRING_OR_NULL_KEYS = frozenset(
    {
        "description",
        "excerpt",
        "featureImage",
        "featureImageAlt",
        "featureImageCaption",
        "twitterLabel1",
        "twitterData1",
    }
)
OPTIONAL_STRING_KEYS = frozenset(
    {
        "seoTitle",
        "ogTitle",
        "ogDescription",
        "ogImage",
        "twitterTitle",
        "twitterDescription",
        "twitterImage",
        "twitterUrl",
        "twitterSite",
        "twitterLabel2",
        "twitterData2",
    }
)
PUBLIC_TAGS = frozenset(
    {
        "open-source",
        "rpa",
        "browser-automation",
        "github",
        "stars",
        "hackernews",
        "front-page",
        "news",
    }
)
RESERVED_SLUG_SEGMENTS = (
    "page",
    "tag",
    "author",
    "rss",
    "sitemap",
    "sitemap-pages",
    "sitemap-posts",
    "sitemap-authors",
    "sitemap-tags",
)
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
AUTHOR_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
UTC_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$")
FOOTNOTE_LABEL_RE = re.compile(r"^[a-z0-9_-]+$")

NEW_HTML_ATTRIBUTES: dict[str, frozenset[str]] = {
    "p": frozenset(),
    "br": frozenset(),
    "strong": frozenset(),
    "em": frozenset(),
    "a": frozenset({"href", "title", "rel"}),
    "ul": frozenset(),
    "ol": frozenset(),
    "li": frozenset(),
    "blockquote": frozenset(),
    "pre": frozenset(),
    "code": frozenset(),
    "h2": frozenset({"id"}),
    "h3": frozenset({"id"}),
    "h4": frozenset({"id"}),
    "table": frozenset(),
    "thead": frozenset(),
    "tbody": frozenset(),
    "tr": frozenset(),
    "th": frozenset({"colspan", "rowspan"}),
    "td": frozenset({"colspan", "rowspan"}),
    "figure": frozenset(),
    "img": frozenset({"src", "alt", "width", "height", "loading"}),
    "figcaption": frozenset(),
    "details": frozenset({"open"}),
    "summary": frozenset(),
}
MIGRATED_HTML_ATTRIBUTES: dict[str, frozenset[str]] = {
    "a": frozenset({"class", "href", "rel", "target"}),
    "b": frozenset(),
    "blockquote": frozenset(),
    "br": frozenset(),
    "button": frozenset({"aria-label", "class"}),
    "code": frozenset({"class", "spellcheck"}),
    "col": frozenset({"style"}),
    "colgroup": frozenset(),
    "div": frozenset({"class"}),
    "em": frozenset(),
    "figcaption": frozenset(),
    "figure": frozenset({"class", "data-kg-custom-thumbnail", "data-kg-thumbnail"}),
    "h1": frozenset({"id"}),
    "h2": frozenset({"id"}),
    "h3": frozenset({"id"}),
    "hr": frozenset(),
    "img": frozenset({"alt", "class", "height", "loading", "sizes", "src", "srcset", "width"}),
    "input": frozenset({"class", "max", "type", "value"}),
    "li": frozenset(),
    "ol": frozenset(),
    "p": frozenset({"class", "style"}),
    "path": frozenset({"d"}),
    "pre": frozenset(),
    "rect": frozenset({"height", "rx", "ry", "width", "x", "y"}),
    "span": frozenset({"class", "data-code-marker", "style"}),
    "strong": frozenset(),
    "svg": frozenset({"viewbox", "xmlns"}),
    "table": frozenset({"class", "data-diff-anchor", "data-paste-markdown-skip", "data-tab-size", "style"}),
    "tbody": frozenset({"style"}),
    "td": frozenset({"class", "colspan", "data-lock-side-selection", "data-split-side", "rowspan", "style"}),
    "th": frozenset({"class", "colspan", "rowspan", "style"}),
    "thead": frozenset(),
    "tr": frozenset({"class", "data-hunk", "style"}),
    "u": frozenset(),
    "ul": frozenset(),
    "video": frozenset(
        {"autoplay", "height", "loop", "muted", "playsinline", "poster", "preload", "src", "style", "width"}
    ),
}
URL_ATTRIBUTES = frozenset({"href", "src", "poster"})
VOID_TAGS = frozenset({"br", "col", "hr", "img", "input"})

# These six hashes identify the ratified CASE 2 migrated passthrough URLs without
# publishing a second URL map. The URLs already appear in the migrated posts.
PASSTHROUGH_URL_SHA256 = frozenset(
    {
        "d577db0e411828dbd4752dc8ff7c3503be50e8c6acdf4b08618d0a6f78d4ec3c",
        "92d0246bf524eab94f29538cfc296e7fdc64ee8cdbec4fc69c936bc3a4c8046d",
        "00361a675a62342541599efd6b7e7e025019916a8ab71205b35dbb7e421a5ba3",
        "8fe2f7616979909200bcaf6aa4d30007a619ef4f9b18f1cb8228cd139cc59c86",
        "03e66ee27180f0912b84601fa0f96092fcb8aea9f29bff0a3ffd18d1f29f3d41",
        "3eab6953fce59d662654c1c3cc5dd68362c1876c2f5c8ceb08de256fa7cace5f",
    }
)

SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(rb"\bsk_(?:live|test)_[A-Za-z0-9]{20,}\b"),
    re.compile(rb"\bre_[A-Za-z0-9]{24,}\b"),
    re.compile(rb"\bphc_[A-Za-z0-9_-]{20,}\b"),
)


def _admit_untrusted_url(_: str) -> bool:
    return True


def _preserve_untrusted_url(url: str) -> str:
    return url


# Match production remarkGfm: tables, footnotes, strikethrough with a single
# tilde, autolink literals via linkify, and native task-list items. Do not
# enable alerts or parser frontmatter because remarkGfm does not add them.
# Raw HTML tag filtering remains stricter in _ContentHTMLParser.
MARKDOWN_PARSER = (
    MarkdownIt(
        "commonmark",
        {
            "html": True,
            "linkify": True,
            "maxNesting": MAX_POST_BYTES,
            "strikethrough_single_tilde": True,
            "tasklists": True,
            "tasklists_editable": False,
        },
    )
    .enable(["table", "strikethrough", "linkify"])
    .use(footnote_plugin, inline=False)
)
MARKDOWN_PARSER.validateLink = _admit_untrusted_url
MARKDOWN_PARSER.normalizeLink = _preserve_untrusted_url
ESM_IMPORT_RE = re.compile(
    r"""(?mx)
    ^[ \t]*import(?:
        [ \t]*["'][^"'\r\n]+["']
        |
        [ \t\r\n]+(?:
            [A-Za-z_$][A-Za-z0-9_$]*(?:[ \t\r\n]*,[ \t\r\n]*)?
        )?(?:
            \*[ \t\r\n]+as[ \t\r\n]+[A-Za-z_$][A-Za-z0-9_$]*
            |
            \{[^}]*\}
        )?[ \t\r\n]+from[ \t\r\n]+["'][^"']+["']
    )[ \t]*;?
    """
)
ESM_EXPORT_RE = re.compile(
    r"""(?mx)
    ^[ \t]*export[ \t\r\n]+(?:
        default\b
        | const\b
        | let\b
        | var\b
        | function\b
        | class\b
        | async[ \t\r\n]+function\b
        | \{
        | \*
    )
    """
)


@dataclass(frozen=True)
class UrlReference:
    url: str
    is_media: bool


@dataclass(frozen=True)
class HtmlOccurrence:
    kind: str
    tag: str
    attributes: tuple[tuple[str, str], ...] = ()
    data: str = ""


@dataclass
class Post:
    path: str
    filename_slug: str
    values: dict[str, object]
    body: str
    urls: list[UrlReference] = field(default_factory=list)
    local_media_references: set[str] = field(default_factory=set)
    template_tokens: list[str] = field(default_factory=list)
    html_occurrences: Counter[HtmlOccurrence] = field(default_factory=Counter)

    @property
    def slug(self) -> str:
        value = self.values.get("slug")
        return value if isinstance(value, str) else self.filename_slug

    @property
    def migrated(self) -> bool:
        return self.values.get("migratedFromGhost") is True


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    error_categories: Counter[str] = field(default_factory=Counter)
    post_count: int = 0
    state_counts: Counter[str] = field(default_factory=Counter)
    unique_pairs: int = 0
    media_file_count: int = 0
    sync_mapping_count: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, path: str, message: str, category: str = "validation") -> None:
        self.errors.append(f"{path}: {message}")
        self.error_categories[category] += 1


@dataclass(frozen=True)
class MediaFacts:
    format: str
    width: int
    height: int
    frame_count: int = 1
    summed_frame_pixels: int | None = None


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _reject_json_constant(_: str) -> object:
    raise ValueError("nonstandard JSON constant")


def _is_string_or_none(value: object) -> bool:
    return value is None or isinstance(value, str)


def _has_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _has_secret_material(data: bytes) -> bool:
    return any(pattern.search(data) for pattern in SECRET_PATTERNS)


def _is_valid_utc_timestamp(value: str) -> bool:
    if not UTC_TIMESTAMP_RE.fullmatch(value):
        return False
    timestamp_format = "%Y-%m-%dT%H:%M:%S.%fZ" if "." in value else "%Y-%m-%dT%H:%M:%SZ"
    try:
        datetime.strptime(value, timestamp_format)
    except ValueError:
        return False
    return True


class _ContentHTMLParser(HTMLParser):
    def __init__(self, path: str, migrated: bool, report: ValidationReport) -> None:
        super().__init__(convert_charrefs=True)
        self.path = path
        self.report = report
        self.allowlist = MIGRATED_HTML_ATTRIBUTES if migrated else NEW_HTML_ATTRIBUTES
        self.references: list[UrlReference] = []
        self.template_fragments: list[str] = []
        self.occurrences: Counter[HtmlOccurrence] = Counter()

    def _add_tag(self, tag: str, attributes: list[tuple[str, str | None]]) -> None:
        allowed_attributes = self.allowlist.get(tag)
        if allowed_attributes is None:
            self.report.add(self.path, "raw HTML tag is not allowed", "forbidden")
            return
        names = [name.lower() for name, _ in attributes]
        if len(names) != len(set(names)):
            self.report.add(self.path, "raw HTML tag has a duplicate attribute", "forbidden")
        for raw_name, raw_value in attributes:
            name = raw_name.lower()
            if name.startswith("on") or name not in allowed_attributes:
                self.report.add(self.path, "raw HTML attribute is not allowed", "forbidden")
                continue
            value = raw_value or ""
            self.template_fragments.append(value)
            if name in URL_ATTRIBUTES:
                self.references.append(UrlReference(value, tag in {"img", "video"} and name != "href"))
            elif name == "srcset":
                for candidate in value.split(","):
                    parts = candidate.strip().split()
                    if parts:
                        self.references.append(UrlReference(parts[0], True))
            elif name == "style":
                if re.search(r"expression\s*\(", value, re.IGNORECASE):
                    self.report.add(self.path, "active CSS expression is not allowed", "forbidden")
                for match in re.finditer(r"url\(\s*(['\"]?)(.*?)\1\s*\)", value, re.IGNORECASE):
                    self.references.append(UrlReference(match.group(2), True))

    @staticmethod
    def _occurrence(kind: str, tag: str, attributes: list[tuple[str, str | None]]) -> HtmlOccurrence:
        return HtmlOccurrence(
            kind,
            tag.lower(),
            tuple((name.lower(), value or "") for name, value in attributes),
        )

    def handle_starttag(self, tag: str, attributes: list[tuple[str, str | None]]) -> None:
        self.occurrences[self._occurrence("start", tag, attributes)] += 1
        self._add_tag(tag.lower(), attributes)

    def handle_startendtag(self, tag: str, attributes: list[tuple[str, str | None]]) -> None:
        self.occurrences[self._occurrence("startend", tag, attributes)] += 1
        self._add_tag(tag.lower(), attributes)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        self.occurrences[HtmlOccurrence("end", lowered)] += 1
        if lowered not in self.allowlist or lowered in VOID_TAGS:
            self.report.add(self.path, "raw HTML closing tag is not allowed", "forbidden")

    def handle_data(self, data: str) -> None:
        self.template_fragments.append(data)

    def handle_comment(self, data: str) -> None:
        self.occurrences[HtmlOccurrence("comment", "", data=data)] += 1
        if self.allowlist is not MIGRATED_HTML_ATTRIBUTES or data.strip() not in {
            "",
            "kg-card-begin: html",
            "kg-card-end: html",
        }:
            self.report.add(self.path, "raw HTML comment is not allowed", "forbidden")

    def handle_decl(self, decl: str) -> None:
        self.report.add(self.path, "raw HTML declaration is not allowed", "forbidden")

    def handle_pi(self, data: str) -> None:
        self.report.add(self.path, "raw HTML processing instruction is not allowed", "forbidden")


def _parse_frontmatter(path: str, data: bytes, report: ValidationReport) -> Post | None:
    if len(data) > MAX_POST_BYTES:
        report.add(path, "post exceeds the byte limit")
        return None
    if data.startswith(b"\xef\xbb\xbf"):
        report.add(path, "UTF-8 byte-order mark is not allowed")
        return None
    if b"\x00" in data:
        report.add(path, "NUL byte is not allowed")
        return None
    if b"\r" in data:
        report.add(path, "non-LF newline is not canonical")
        return None
    if _has_secret_material(data):
        report.add(path, "possible secret material is not allowed", "secret")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        report.add(path, "file is not valid UTF-8")
        return None
    lines = text.splitlines(keepends=True)
    if not lines or lines[0] != "---\n":
        report.add(path, "frontmatter opening delimiter is malformed")
        return None
    closing_index = next((index for index, line in enumerate(lines[1:], start=1) if line == "---\n"), None)
    if closing_index is None:
        report.add(path, "frontmatter closing delimiter is missing or malformed")
        return None

    values: dict[str, object] = {}
    key_order: list[str] = []
    for line in lines[1:closing_index]:
        raw_line = line[:-1]
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9]*): (.+)", raw_line)
        if not match:
            report.add(path, "frontmatter line is not canonical")
            continue
        key, raw_value = match.groups()
        if key in values:
            report.add(path, "frontmatter key is duplicated")
            continue
        if key not in ALLOWED_KEYS:
            report.add(path, "frontmatter key is not in the public schema")
            continue
        try:
            value = json.loads(raw_value, parse_constant=_reject_json_constant)
        except (ValueError, RecursionError):
            report.add(path, "frontmatter value is not valid canonical JSON")
            continue
        if isinstance(value, dict) or _canonical_json(value) != raw_value:
            report.add(path, "frontmatter value encoding is not canonical")
        values[key] = value
        key_order.append(key)

    missing = [key for key in REQUIRED_KEYS if key not in values]
    if missing:
        report.add(path, "required frontmatter key is missing")
    if tuple(key_order[: len(REQUIRED_KEYS)]) != REQUIRED_KEYS:
        report.add(path, "required frontmatter keys are not in canonical order")
    optional_order = [key for key in key_order if key in OPTIONAL_KEYS]
    if optional_order != sorted(optional_order, key=OPTIONAL_KEYS.index):
        report.add(path, "optional frontmatter keys are not in canonical order")

    filename_slug = PurePosixPath(path).stem
    post = Post(path=path, filename_slug=filename_slug, values=values, body="".join(lines[closing_index + 1 :]))
    _validate_frontmatter_types(post, report)
    return post


def _validate_frontmatter_types(post: Post, report: ValidationReport) -> None:
    values = post.values
    path = post.path
    for key in ("title", "slug", "author"):
        if key in values and not isinstance(values[key], str):
            report.add(path, "frontmatter field has the wrong type")
    for key in STRING_OR_NULL_KEYS:
        if key in values and not _is_string_or_none(values[key]):
            report.add(path, "frontmatter field has the wrong type")
    for key in OPTIONAL_STRING_KEYS:
        if key in values and not isinstance(values[key], str):
            report.add(path, "optional frontmatter field has the wrong type")
    if "twitterCard" in values and values["twitterCard"] not in {"summary", "summary_large_image"}:
        report.add(path, "twitterCard has an invalid value")
    for key in ("publishedAt", "updatedAt"):
        value = values.get(key)
        if value is not None and (not isinstance(value, str) or not _is_valid_utc_timestamp(value)):
            report.add(path, "timestamp is not an ISO-8601 UTC string or null")
    if "tags" in values and (
        not isinstance(values["tags"], list) or any(not isinstance(item, str) for item in values["tags"])
    ):
        report.add(path, "tags must be an array of strings")
    for key in ("sendNewsletter", "migratedFromGhost"):
        if key in values and not isinstance(values[key], bool):
            report.add(path, "frontmatter boolean has the wrong type")
    if ("twitterLabel2" in values) != ("twitterData2" in values):
        report.add(path, "twitterLabel2 and twitterData2 must be present together")


def _validate_frontmatter_semantics(post: Post, is_new: bool, bootstrap: bool, report: ValidationReport) -> None:
    values = post.values
    path = post.path
    slug = values.get("slug")
    migrated = values.get("migratedFromGhost")
    state = values.get("publicationState")
    send_newsletter = values.get("sendNewsletter")

    if isinstance(values.get("title"), str) and not values["title"].strip():
        report.add(path, "title must contain visible text")
    if isinstance(slug, str) and slug != post.filename_slug:
        report.add(path, "frontmatter slug does not match the filename")
    if isinstance(values.get("author"), str) and (
        len(values["author"]) > 64 or not AUTHOR_RE.fullmatch(values["author"])
    ):
        report.add(path, "author key is invalid")
    if state not in {"published", "sent", "draft"}:
        report.add(path, "publicationState is invalid")
    if state in {"published", "sent"} and values.get("publishedAt") is None:
        report.add(path, "published or sent post requires publishedAt")
    if state == "draft" and values.get("publishedAt") is not None:
        report.add(path, "draft post must use publishedAt null")
    if state in {"draft", "sent"} and send_newsletter is not False:
        report.add(path, "draft or sent post must not send a newsletter")
    if migrated is True and send_newsletter is not False:
        report.add(path, "migrated post must not send a newsletter")
    if migrated is False and state == "published" and send_newsletter is not True:
        report.add(path, "new published post must set sendNewsletter true")
    if (
        migrated is False
        and state == "published"
        and (not isinstance(values.get("description"), str) or not values["description"].strip())
    ):
        report.add(path, "new published post requires a nonempty description")
    if migrated is False and state == "draft" and send_newsletter is not False:
        report.add(path, "new draft must set sendNewsletter false")
    if is_new and not bootstrap and state == "sent":
        report.add(path, "a new sent post is not allowed")
    if is_new and not bootstrap and migrated is not False:
        report.add(path, "a new post cannot claim migratedFromGhost")
    if migrated is False and isinstance(slug, str):
        reserved = any(slug == segment or slug.startswith(f"{segment}-") for segment in RESERVED_SLUG_SEGMENTS)
        if len(slug) > 100 or not SLUG_RE.fullmatch(slug) or reserved:
            report.add(path, "new post slug is invalid or reserved")
        tags = values.get("tags")
        if isinstance(tags, list) and any(tag not in PUBLIC_TAGS for tag in tags if isinstance(tag, str)):
            report.add(path, "new post uses a tag outside the public registry")


def _validate_canonical_footnote_labels(post: Post, report: ValidationReport) -> None:
    """Fail closed where the Python plugin and production normalize differently."""
    cursor = 0
    definition_labels: set[str] = set()
    while True:
        opening = post.body.find("[^", cursor)
        if opening < 0:
            return
        closing = post.body.find("]", opening + 2)
        if closing < 0:
            report.add(post.path, "footnote label is not canonical lowercase ASCII", "forbidden")
            return
        label = post.body[opening + 2 : closing]
        canonical = FOOTNOTE_LABEL_RE.fullmatch(label) is not None
        if not canonical:
            report.add(post.path, "footnote label is not canonical lowercase ASCII", "forbidden")
        is_definition = closing + 1 < len(post.body) and post.body[closing + 1] == ":"
        if canonical and is_definition:
            if label in definition_labels:
                report.add(post.path, "duplicate footnote definition is not allowed", "forbidden")
            definition_labels.add(label)
        cursor = closing + 1


def _analyze_content(post: Post, report: ValidationReport) -> None:
    references: list[UrlReference] = []
    template_fragments: list[str] = []
    template_surfaces: list[str] = []
    active_markdown_fragments: list[str] = []
    html_parser = _ContentHTMLParser(post.path, post.migrated, report)
    _validate_canonical_footnote_labels(post, report)

    try:
        markdown_tokens = MARKDOWN_PARSER.parse(post.body)
    except Exception:  # noqa: BLE001 - untrusted Markdown must fail closed
        report.add(post.path, "Markdown cannot be parsed safely", "forbidden")
        markdown_tokens = []

    def collect_token(token: object, *, active_markdown: bool) -> str:
        token_type = getattr(token, "type", "")
        content = getattr(token, "content", "")
        attrs = getattr(token, "attrs", None) or {}
        children = getattr(token, "children", None) or ()

        if token_type in {"html_block", "html_inline"}:
            fragment_start = len(html_parser.template_fragments)
            html_parser.feed(content)
            template_surfaces.append("".join(html_parser.template_fragments[fragment_start:]))
            return ""
        if isinstance(content, str) and content:
            template_fragments.append(content)
            if active_markdown and token_type == "text":
                active_markdown_fragments.append(content)
        for name, value in attrs.items():
            if isinstance(value, str):
                template_fragments.append(value)
            if name == "href" and isinstance(value, str):
                references.append(UrlReference(value, False))
            elif name == "src" and token_type == "image" and isinstance(value, str):
                references.append(UrlReference(value, True))
        rendered_children = "".join(
            collect_token(child, active_markdown=active_markdown and token_type != "image") for child in children
        )
        if rendered_children:
            return rendered_children
        if token_type in {"text", "code_inline", "code_block", "fence", "image"}:
            return content
        if token_type in {"softbreak", "hardbreak"}:
            return "\n"
        return ""

    try:
        for block_token in markdown_tokens:
            rendered_surface = collect_token(
                block_token,
                active_markdown=block_token.type == "inline",
            )
            if rendered_surface:
                template_surfaces.append(rendered_surface)
        html_parser.close()
    except (AssertionError, ValueError):
        report.add(post.path, "raw HTML is malformed", "forbidden")

    active_markdown_text = "\n".join(active_markdown_fragments)
    if ESM_IMPORT_RE.search(active_markdown_text) or ESM_EXPORT_RE.search(active_markdown_text):
        report.add(post.path, "MDX import or export is not allowed", "forbidden")
    if re.search(r"<[A-Z][A-Za-z0-9]*(?:\s|/?>)", active_markdown_text):
        report.add(post.path, "JSX component is not allowed", "forbidden")

    references.extend(html_parser.references)
    template_fragments.extend(html_parser.template_fragments)
    post.html_occurrences.update(html_parser.occurrences)

    for value in post.values.values():
        if isinstance(value, str):
            template_fragments.append(value)
        elif isinstance(value, list):
            template_fragments.extend(item for item in value if isinstance(item, str))

    for key in ("featureImage", "ogImage", "twitterImage"):
        value = post.values.get(key)
        if isinstance(value, str):
            references.append(UrlReference(value, True))
    twitter_url = post.values.get("twitterUrl")
    if isinstance(twitter_url, str):
        references.append(UrlReference(twitter_url, False))

    caption = post.values.get("featureImageCaption")
    if isinstance(caption, str) and "<" in caption:
        caption_parser = _ContentHTMLParser(post.path, post.migrated, report)
        try:
            caption_parser.feed(caption)
            caption_parser.close()
        except (AssertionError, ValueError):
            report.add(post.path, "feature image caption HTML is malformed", "forbidden")
        references.extend(caption_parser.references)
        template_surfaces.append("".join(caption_parser.template_fragments))
        template_fragments.extend(caption_parser.template_fragments)
        post.html_occurrences.update(caption_parser.occurrences)

    template_surfaces.extend(template_fragments)
    post.template_tokens = [
        match.group(0)
        for surface in template_surfaces
        for match in re.finditer(r"\{\{\{?.*?\}\}\}?", surface, re.DOTALL)
    ]
    post.urls = references


def _parse_whatwg_ipv4_number(part: str) -> int | None:
    if not part:
        return None
    base = 10
    digits = part
    if part.lower().startswith("0x"):
        base = 16
        digits = part[2:]
    elif len(part) > 1 and part.startswith("0"):
        base = 8
        digits = part[1:]
    if not digits:
        return 0
    try:
        return int(digits, base)
    except ValueError:
        return None


def _normalize_whatwg_ipv4(hostname: str) -> ipaddress.IPv4Address | None:
    candidate = unquote(hostname).rstrip(".").lower()
    parts = candidate.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    numbers = [_parse_whatwg_ipv4_number(part) for part in parts]
    if any(number is None for number in numbers):
        return None
    values = [number for number in numbers if number is not None]
    for number in values[:-1]:
        if number > 255:
            return None
    last_limit = 256 ** (5 - len(values))
    if values[-1] >= last_limit:
        return None
    numeric = values[-1]
    for index, number in enumerate(values[:-1]):
        numeric += number * (256 ** (3 - index))
    try:
        return ipaddress.IPv4Address(numeric)
    except ipaddress.AddressValueError:
        return None


def _normalize_hostname(hostname: str) -> str | None:
    candidate = unquote(hostname).rstrip(".").lower()
    if ":" in candidate:
        return candidate
    try:
        return candidate.encode("idna").decode("ascii")
    except UnicodeError:
        return None


def _url_has_private_destination(hostname: str) -> bool:
    lowered = hostname
    if (
        lowered in {"localhost", "localhost.localdomain"}
        or lowered.endswith(".localhost")
        or lowered.endswith(".local")
    ):
        return True
    whatwg_ipv4 = _normalize_whatwg_ipv4(lowered)
    if whatwg_ipv4 is not None:
        return not whatwg_ipv4.is_global
    try:
        address = ipaddress.ip_address(lowered.strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    return not address.is_global


def _is_internal_blog_url(parsed_hostname: str | None, path: str) -> bool:
    if parsed_hostname not in {None, "skyvern.com", "www.skyvern.com"}:
        return False
    return path == "/blog" or path.startswith("/blog/")


def _requires_trailing_slash(path: str) -> bool:
    if path.endswith("/") or path.startswith("/blog/media/"):
        return False
    final_segment = path.rsplit("/", 1)[-1]
    return "." not in final_segment


def _is_preserved_exception(
    reference: UrlReference,
    baseline_counts: Counter[tuple[str, bool]],
) -> bool:
    key = (reference.url, reference.is_media)
    if baseline_counts[key] > 0:
        baseline_counts[key] -= 1
        return True
    return False


def _validate_urls(
    post: Post,
    baseline_urls: Counter[tuple[str, bool]],
    bootstrap: bool,
    media_origins: tuple[str, ...],
    pre_apply: bool,
    report: ValidationReport,
) -> None:
    for reference in post.urls:
        url = reference.url.strip()
        if not url or url != reference.url or _has_control_characters(url) or "\\" in url:
            report.add(post.path, "URL contains whitespace, a control character, or a backslash", "link")
            continue
        try:
            if any(byte < 32 or byte == 127 for byte in unquote_to_bytes(url)):
                report.add(post.path, "URL contains an encoded control character", "link")
                continue
        except UnicodeEncodeError:
            report.add(post.path, "URL is not canonically encoded", "link")
            continue
        if url.startswith("//"):
            report.add(post.path, "protocol-relative URL is not allowed", "link")
            continue
        if re.match(r"^(?:[A-Za-z]:[\\/]|/(?:Users|home|etc|tmp|var|opt|root)/)", url):
            report.add(post.path, "absolute filesystem path is not allowed", "link")
            continue

        try:
            parsed = urlsplit(url)
            scheme = parsed.scheme.lower()
            hostname = _normalize_hostname(parsed.hostname) if parsed.hostname else None
        except ValueError:
            report.add(post.path, "URL cannot be parsed safely", "link")
            continue
        if parsed.hostname is not None and hostname is None:
            report.add(post.path, "URL hostname is not canonically encodable", "link")
            continue
        if parsed.username is not None or parsed.password is not None:
            report.add(post.path, "credentials in a URL are not allowed", "link")
            continue
        if scheme in {"javascript", "vbscript", "data", "file"}:
            report.add(post.path, "active or local URL scheme is not allowed", "link")
            continue
        if scheme and scheme not in {"https", "http", "mailto"}:
            report.add(post.path, "URL scheme is not allowed", "link")
            continue
        if hostname and _url_has_private_destination(hostname):
            if _is_preserved_exception(reference, baseline_urls):
                continue
            report.add(post.path, "local or private network URL is not allowed", "link")
            continue
        if hostname == "skyvern.ghost.io" or (
            hostname is not None
            and "/content/images/" in parsed.path
            and (hostname.endswith(".ghost.io") or "ghost" in hostname)
        ):
            report.add(post.path, "Ghost origin or storage URL is not allowed", "link")
            continue
        http_preserved = False
        if scheme == "http":
            http_preserved = _is_preserved_exception(reference, baseline_urls)
            if not http_preserved:
                report.add(post.path, "new HTTP URL is not allowed", "link")
                continue
        if scheme == "mailto":
            if reference.is_media or not parsed.path or parsed.netloc:
                report.add(post.path, "mailto URL is invalid in this context", "link")
            continue

        if reference.is_media:
            if scheme == "" and not parsed.netloc:
                expected_prefix = f"./media/{post.slug}/"
                if (
                    not url.startswith(expected_prefix)
                    or parsed.query
                    or parsed.fragment
                    or PurePosixPath(parsed.path).name in {"", ".", ".."}
                    or ".." in PurePosixPath(parsed.path).parts
                ):
                    report.add(post.path, "local media reference does not use its owning slug", "media")
                else:
                    post.local_media_references.add(parsed.path)
                continue
            if not post.migrated:
                report.add(post.path, "new post cannot hotlink media", "media")
                continue
            url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
            is_passthrough = url_hash in PASSTHROUGH_URL_SHA256
            if not is_passthrough and (parsed.query or parsed.fragment or ".." in PurePosixPath(parsed.path).parts):
                report.add(post.path, "migrated media URL is not an immutable object path", "media")
                continue
            allowed_origin = any(url.startswith(f"{origin}/media/blog/") for origin in media_origins)
            allowed_placeholder = pre_apply and url.startswith("https://DOMAIN/media/blog/")
            if not (allowed_origin or allowed_placeholder or is_passthrough):
                report.add(post.path, "migrated media URL is outside the approved immutable origins", "media")
                continue
            if allowed_placeholder and hostname != "domain":
                report.add(post.path, "pre-apply media placeholder is malformed", "media")
            continue

        if not scheme and not parsed.netloc and not (url.startswith("/") or url.startswith("#")):
            report.add(post.path, "relative non-media URL is not allowed", "link")
            continue
        if hostname == "domain":
            report.add(post.path, "media placeholder cannot be used as a normal link", "link")
            continue
        if _is_internal_blog_url(hostname, parsed.path) and _requires_trailing_slash(parsed.path):
            preserved = http_preserved or _is_preserved_exception(reference, baseline_urls)
            if not preserved:
                report.add(post.path, "new internal blog link is missing its trailing slash", "link")


def _png_facts(data: bytes) -> MediaFacts:
    if len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("invalid PNG header")
    offset = 8
    width = height = 0
    found_iend = False
    animated = False
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > len(data):
            raise ValueError("truncated PNG chunk")
        chunk_data = data[offset + 8 : offset + 8 + length]
        if chunk_type == b"IHDR":
            if offset != 8 or length != 13:
                raise ValueError("invalid PNG IHDR")
            width, height = struct.unpack(">II", chunk_data[:8])
        elif chunk_type == b"acTL":
            animated = True
        elif chunk_type == b"IEND":
            found_iend = True
            break
        offset = chunk_end
    if not found_iend or width <= 0 or height <= 0:
        raise ValueError("PNG dimensions or terminator missing")
    if animated:
        raise ValueError("APNG is not allowed")
    return MediaFacts("png", width, height)


def _jpeg_facts(data: bytes) -> MediaFacts:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise ValueError("invalid JPEG header")
    sof_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    offset = 2
    while offset < len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in {0x00, 0x01, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data):
            raise ValueError("truncated JPEG segment")
        length = struct.unpack(">H", data[offset : offset + 2])[0]
        if length < 2 or offset + length > len(data):
            raise ValueError("invalid JPEG segment length")
        if marker in sof_markers:
            if length < 7:
                raise ValueError("invalid JPEG frame header")
            height, width = struct.unpack(">HH", data[offset + 3 : offset + 7])
            if width <= 0 or height <= 0:
                raise ValueError("invalid JPEG dimensions")
            return MediaFacts("jpeg", width, height)
        if marker == 0xDA:
            break
        offset += length
    raise ValueError("JPEG dimensions missing")


def _skip_gif_subblocks(data: bytes, offset: int) -> int:
    while True:
        if offset >= len(data):
            raise ValueError("truncated GIF sub-block")
        length = data[offset]
        offset += 1
        if length == 0:
            return offset
        if offset + length > len(data):
            raise ValueError("truncated GIF sub-block data")
        offset += length


def _gif_facts(data: bytes) -> MediaFacts:
    if len(data) < 14 or data[:6] not in {b"GIF87a", b"GIF89a"}:
        raise ValueError("invalid GIF header")
    width, height = struct.unpack("<HH", data[6:10])
    if width <= 0 or height <= 0:
        raise ValueError("invalid GIF dimensions")
    packed = data[10]
    offset = 13
    if packed & 0x80:
        offset += 3 * (2 ** ((packed & 0x07) + 1))
    frame_count = 0
    found_trailer = False
    while offset < len(data):
        introducer = data[offset]
        offset += 1
        if introducer == 0x3B:
            found_trailer = True
            break
        if introducer == 0x21:
            if offset >= len(data):
                raise ValueError("truncated GIF extension")
            offset += 1
            offset = _skip_gif_subblocks(data, offset)
            continue
        if introducer != 0x2C or offset + 9 > len(data):
            raise ValueError("invalid GIF block")
        frame_left, frame_top, frame_width, frame_height = struct.unpack("<HHHH", data[offset : offset + 8])
        frame_packed = data[offset + 8]
        offset += 9
        if (
            frame_width <= 0
            or frame_height <= 0
            or frame_left + frame_width > width
            or frame_top + frame_height > height
        ):
            raise ValueError("invalid GIF frame dimensions")
        if frame_packed & 0x80:
            offset += 3 * (2 ** ((frame_packed & 0x07) + 1))
        if offset >= len(data):
            raise ValueError("truncated GIF image data")
        offset += 1
        offset = _skip_gif_subblocks(data, offset)
        frame_count += 1
    if not found_trailer or frame_count == 0:
        raise ValueError("GIF frame or terminator missing")
    canvas_pixels = width * height
    return MediaFacts("gif", width, height, frame_count, canvas_pixels * frame_count)


def _webp_facts(data: bytes) -> MediaFacts:
    if len(data) < 20 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("invalid WebP header")
    declared_size = struct.unpack("<I", data[4:8])[0] + 8
    if declared_size > len(data):
        raise ValueError("truncated WebP RIFF")
    offset = 12
    width = height = 0
    animated = False
    while offset + 8 <= declared_size:
        chunk_type = data[offset : offset + 4]
        chunk_size = struct.unpack("<I", data[offset + 4 : offset + 8])[0]
        chunk_start = offset + 8
        chunk_end = chunk_start + chunk_size
        if chunk_end > declared_size:
            raise ValueError("truncated WebP chunk")
        chunk = data[chunk_start:chunk_end]
        if chunk_type == b"VP8X":
            if len(chunk) < 10:
                raise ValueError("invalid WebP VP8X chunk")
            animated = bool(chunk[0] & 0x02)
            width = 1 + int.from_bytes(chunk[4:7], "little")
            height = 1 + int.from_bytes(chunk[7:10], "little")
        elif chunk_type == b"VP8 ":
            if len(chunk) < 10 or chunk[3:6] != b"\x9d\x01\x2a":
                raise ValueError("invalid WebP VP8 frame")
            width = struct.unpack("<H", chunk[6:8])[0] & 0x3FFF
            height = struct.unpack("<H", chunk[8:10])[0] & 0x3FFF
        elif chunk_type == b"VP8L":
            if len(chunk) < 5 or chunk[0] != 0x2F:
                raise ValueError("invalid WebP VP8L frame")
            bits = int.from_bytes(chunk[1:5], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
        elif chunk_type in {b"ANIM", b"ANMF"}:
            animated = True
        offset = chunk_end + (chunk_size & 1)
    if animated:
        raise ValueError("animated WebP is not allowed")
    if width <= 0 or height <= 0:
        raise ValueError("WebP dimensions missing")
    return MediaFacts("webp", width, height)


def _read_media_facts(data: bytes) -> MediaFacts:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _png_facts(data)
    if data.startswith(b"\xff\xd8"):
        return _jpeg_facts(data)
    if data.startswith((b"GIF87a", b"GIF89a")):
        return _gif_facts(data)
    if data.startswith(b"RIFF"):
        return _webp_facts(data)
    raise ValueError("unsupported media magic bytes")


def _validate_media_file(path: Path, relative_path: str, report: ValidationReport) -> None:
    suffix = path.suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        report.add(relative_path, "media extension is not allowed", "media")
        return
    size = path.stat().st_size
    if size > MAX_MEDIA_FILE_BYTES:
        report.add(relative_path, "media file exceeds the encoded byte limit", "media")
        return
    data = path.read_bytes()
    if _has_secret_material(data):
        report.add(relative_path, "possible secret material", "secret")
    try:
        facts = _read_media_facts(data)
    except ValueError:
        report.add(relative_path, "media header, chunks, or dimensions are invalid", "media")
        return
    expected_format = "jpeg" if suffix in {".jpg", ".jpeg"} else suffix[1:]
    if facts.format != expected_format:
        report.add(relative_path, "media extension does not match magic bytes", "media")
        return
    if facts.format == "gif":
        frame_pixels = facts.width * facts.height
        if (
            facts.width > MAX_GIF_AXIS
            or facts.height > MAX_GIF_AXIS
            or facts.frame_count > MAX_GIF_FRAMES
            or frame_pixels > MAX_GIF_FRAME_PIXELS
            or (facts.summed_frame_pixels or 0) > MAX_GIF_SUMMED_PIXELS
        ):
            report.add(relative_path, "GIF dimension, frame, or decoded-pixel budget is exceeded", "media")
    elif (
        facts.width > MAX_STATIC_AXIS
        or facts.height > MAX_STATIC_AXIS
        or facts.width * facts.height > MAX_STATIC_PIXELS
    ):
        report.add(relative_path, "static-image dimension or decoded-pixel budget is exceeded", "media")


def _walk_repository(root: Path, report: ValidationReport) -> tuple[list[Path], dict[str, list[Path]]]:
    blogs = root / "blogs"
    if not blogs.is_dir() or blogs.is_symlink():
        report.add("blogs/", "blog directory is missing or is a symlink")
        return [], {}
    posts: list[Path] = []
    media_by_slug: dict[str, list[Path]] = {}
    for directory, directory_names, file_names in os.walk(blogs, followlinks=False):
        directory_path = Path(directory)
        relative_directory = directory_path.relative_to(blogs)
        for name in list(directory_names):
            child = directory_path / name
            child_relative = child.relative_to(root).as_posix()
            if child.is_symlink():
                report.add(child_relative, "symlink directory is not allowed", "media")
                directory_names.remove(name)
        if relative_directory == Path("."):
            for name in directory_names:
                if name != "media":
                    report.add((directory_path / name).relative_to(root).as_posix(), "unexpected blog subdirectory")
            for name in file_names:
                path = directory_path / name
                relative = path.relative_to(root).as_posix()
                if path.is_symlink() or not path.is_file():
                    report.add(relative, "post path must be a regular file")
                elif name == "README.md":
                    readme_data = path.read_bytes()
                    if len(readme_data) > MAX_POST_BYTES:
                        report.add(relative, "blog contributor guide exceeds the byte limit")
                    if readme_data.startswith(b"\xef\xbb\xbf") or b"\x00" in readme_data:
                        report.add(relative, "blog contributor guide is not canonical UTF-8 text")
                    if _has_secret_material(readme_data):
                        report.add(relative, "possible secret material is not allowed", "secret")
                    try:
                        readme_data.decode("utf-8")
                    except UnicodeDecodeError:
                        report.add(relative, "blog contributor guide is not valid UTF-8")
                    continue
                elif path.suffix != ".md":
                    report.add(relative, "only Markdown posts are allowed at the blog root")
                else:
                    posts.append(path)
        elif relative_directory == Path("media"):
            if file_names:
                for name in file_names:
                    report.add(
                        (directory_path / name).relative_to(root).as_posix(),
                        "media file must belong to a slug",
                        "media",
                    )
        elif len(relative_directory.parts) == 2 and relative_directory.parts[0] == "media":
            slug = relative_directory.parts[1]
            if directory_names:
                for name in directory_names:
                    report.add(
                        (directory_path / name).relative_to(root).as_posix(),
                        "nested media directory is not allowed",
                        "media",
                    )
            media_by_slug.setdefault(slug, [])
            for name in file_names:
                path = directory_path / name
                relative = path.relative_to(root).as_posix()
                if path.is_symlink():
                    report.add(relative, "media symlink is not allowed", "media")
                    continue
                try:
                    mode = path.stat().st_mode
                except FileNotFoundError:
                    report.add(relative, "media file disappeared during validation", "media")
                    continue
                if not stat.S_ISREG(mode):
                    report.add(relative, "media path must be a regular file", "media")
                    continue
                if mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
                    report.add(relative, "executable media file is not allowed", "media")
                media_by_slug[slug].append(path)
        else:
            report.add(directory_path.relative_to(root).as_posix(), "content is outside the canonical blog layout")
    return sorted(posts), media_by_slug


def _parse_posts_from_bytes(files: dict[str, bytes], report: ValidationReport) -> dict[str, Post]:
    posts: dict[str, Post] = {}
    for path, data in sorted(files.items()):
        if path == "blogs/README.md" or not path.endswith(".md"):
            continue
        post = _parse_frontmatter(path, data, report)
        if post is not None:
            posts[post.filename_slug] = post
    return posts


def _load_json_object(path: Path, relative_path: str, report: ValidationReport) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_json_constant)
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        report.add(relative_path, "JSON contract is missing or malformed")
        return {}
    if not isinstance(value, dict):
        report.add(relative_path, "JSON contract must be an object")
        return {}
    return value


def _load_content_contract(root: Path, report: ValidationReport) -> dict[str, object]:
    contract = _load_json_object(root / CONTRACT_PATH, CONTRACT_PATH, report)
    expected_keys = {
        "schemaVersion",
        "bootstrapComplete",
        "approvedMediaOrigins",
        "bootstrapHashLedger",
        "publishedSlugLedger",
    }
    if set(contract) != expected_keys:
        report.add(CONTRACT_PATH, "content contract has unexpected or missing keys")
    if contract.get("schemaVersion") != 1:
        report.add(CONTRACT_PATH, "content contract schema version is invalid")
    if not isinstance(contract.get("bootstrapComplete"), bool):
        report.add(CONTRACT_PATH, "bootstrapComplete must be a boolean")
    origins = contract.get("approvedMediaOrigins")
    if not isinstance(origins, list) or any(not isinstance(origin, str) for origin in origins):
        report.add(CONTRACT_PATH, "approvedMediaOrigins must be an array of strings")
    if contract.get("bootstrapHashLedger") != "scripts/blog_content_bootstrap_hashes.json":
        report.add(CONTRACT_PATH, "bootstrap hash-ledger path is invalid")
    if contract.get("publishedSlugLedger") != "scripts/blog_content_published_slugs.txt":
        report.add(CONTRACT_PATH, "published slug-ledger path is invalid")
    return contract


def _load_bootstrap_hashes(root: Path, contract: dict[str, object], report: ValidationReport) -> dict[str, str]:
    ledger_path = contract.get("bootstrapHashLedger")
    if not isinstance(ledger_path, str):
        return {}
    ledger = _load_json_object(root / ledger_path, ledger_path, report)
    if set(ledger) != {"schemaVersion", "posts"} or ledger.get("schemaVersion") != 1:
        report.add(ledger_path, "bootstrap hash ledger has an invalid schema")
        return {}
    rows = ledger.get("posts")
    if not isinstance(rows, list):
        report.add(ledger_path, "bootstrap hash ledger posts must be an array")
        return {}
    expected: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"slug", "path", "sha256"}:
            report.add(ledger_path, "bootstrap hash row is malformed")
            continue
        slug, path, sha256 = row.get("slug"), row.get("path"), row.get("sha256")
        if (
            not isinstance(slug, str)
            or not isinstance(path, str)
            or path != f"blogs/{slug}.md"
            or not isinstance(sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", sha256)
            or path in expected
        ):
            report.add(ledger_path, "bootstrap hash row is noncanonical or duplicated")
            continue
        expected[path] = sha256
    if len(expected) != BOOTSTRAP_POST_COUNT:
        report.add(ledger_path, "bootstrap hash ledger must contain exactly 277 posts")
    return expected


def _authenticate_bootstrap_files(
    root: Path,
    files: dict[str, bytes],
    contract: dict[str, object],
    report: ValidationReport,
) -> bool:
    expected = _load_bootstrap_hashes(root, contract, report)
    if set(files) != set(expected):
        report.add("blogs/", "bootstrap files do not exactly match the frozen hash ledger")
        return False
    mismatches = [path for path, data in files.items() if hashlib.sha256(data).hexdigest() != expected.get(path)]
    for path in mismatches:
        report.add(path, "post bytes do not match the frozen bootstrap hash ledger")
    return bool(expected) and not mismatches


def _load_published_slug_ledger(root: Path, contract: dict[str, object], report: ValidationReport) -> list[str]:
    ledger_path = contract.get("publishedSlugLedger")
    if not isinstance(ledger_path, str):
        return []
    try:
        text = (root / ledger_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        report.add(ledger_path, "published slug ledger is missing or unreadable")
        return []
    slugs = text.splitlines()
    if text != "".join(f"{slug}\n" for slug in slugs):
        report.add(ledger_path, "published slug ledger is not canonical LF text")
    if slugs != sorted(set(slugs)) or any(not SLUG_RE.fullmatch(slug) for slug in slugs):
        report.add(ledger_path, "published slug ledger must be sorted, unique, and canonical")
    return slugs


def _validate_guard_integrity(
    root: Path,
    base_root: Path | None,
    contract: dict[str, object],
    bootstrap: bool,
    report: ValidationReport,
) -> None:
    if base_root is None or not (base_root / "scripts/validate_blog_content.py").is_file():
        return
    for relative_path in IMMUTABLE_GUARD_PATHS:
        try:
            current = (root / relative_path).read_bytes()
            baseline = (base_root / relative_path).read_bytes()
        except OSError:
            report.add(relative_path, "protected blog guard file is missing")
            continue
        if current != baseline:
            report.add(relative_path, "protected blog guard differs from the trusted base")

    base_contract = _load_content_contract(base_root, report)
    if base_contract.get("bootstrapComplete") is True and contract != base_contract:
        report.add(CONTRACT_PATH, "completed blog content contract is immutable")
    if base_contract.get("bootstrapComplete") is True:
        ledger_path = "scripts/blog_content_bootstrap_hashes.json"
        try:
            if (root / ledger_path).read_bytes() != (base_root / ledger_path).read_bytes():
                report.add(ledger_path, "completed bootstrap hash ledger is immutable")
        except OSError:
            report.add(ledger_path, "completed bootstrap hash ledger is missing")
    if bootstrap:
        report.add("validator configuration", "bootstrap mode is disabled after the initial corpus merge")


def _validate_published_slug_history(
    posts: dict[str, Post],
    current_slugs: list[str],
    base_root: Path | None,
    contract: dict[str, object],
    report: ValidationReport,
) -> None:
    current_set = set(current_slugs)
    published_set = {slug for slug, post in posts.items() if post.values.get("publicationState") == "published"}
    if current_set != published_set:
        report.add(
            str(contract.get("publishedSlugLedger", "published slug ledger")),
            "published slug ledger must exactly match every current published post",
        )
    if base_root is not None and (base_root / CONTRACT_PATH).is_file():
        base_contract = _load_content_contract(base_root, ValidationReport())
        base_slugs = set(_load_published_slug_ledger(base_root, base_contract, ValidationReport()))
        if not base_slugs.issubset(current_set):
            report.add(
                str(contract.get("publishedSlugLedger", "published slug ledger")),
                "published slug tombstones cannot be removed",
            )


def _load_base_from_directory(base_root: Path, report: ValidationReport) -> dict[str, Post]:
    blogs = base_root / "blogs"
    if not blogs.is_dir():
        return {}
    files = {path.relative_to(base_root).as_posix(): path.read_bytes() for path in blogs.glob("*.md")}
    base_report = ValidationReport()
    posts = _parse_posts_from_bytes(files, base_report)
    if base_report.errors:
        report.add("blogs/", "base blog corpus cannot be parsed safely")
    return posts


def _load_base_from_git(root: Path, base_ref: str, report: ValidationReport) -> dict[str, Post]:
    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{base_ref}:blogs"],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if exists.returncode != 0:
        return {}
    archive = subprocess.run(
        ["git", "archive", "--format=tar", base_ref, "blogs"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if archive.returncode != 0:
        report.add("blogs/", "failed to read the pull-request base corpus")
        return {}
    files: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=BytesIO(archive.stdout), mode="r:") as tar:
            for member in tar.getmembers():
                if member.isfile() and member.name.startswith("blogs/") and member.name.endswith(".md"):
                    extracted = tar.extractfile(member)
                    if extracted is not None:
                        files[member.name] = extracted.read()
    except tarfile.TarError:
        report.add("blogs/", "pull-request base archive is malformed")
        return {}
    base_report = ValidationReport()
    posts = _parse_posts_from_bytes(files, base_report)
    if base_report.errors:
        report.add("blogs/", "pull-request base blog corpus cannot be parsed safely")
    return posts


def _validate_sync_mapping(root: Path, report: ValidationReport) -> None:
    path = root / ".github" / "sync.yml"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        report.add(".github/sync.yml", "sync manifest is missing or unreadable")
        return

    mappings: list[tuple[str, str, bool]] = []
    malformed = not lines or lines[0] != "Skyvern-AI/skyvern-cloud:"
    index = 1
    while not malformed and index < len(lines):
        line = lines[index]
        if not line:
            index += 1
            continue
        source_match = re.fullmatch(r"  - source: (\S+)", line)
        if source_match is None or index + 1 >= len(lines):
            malformed = True
            break
        dest_match = re.fullmatch(r"    dest: (\S+)", lines[index + 1])
        if dest_match is None:
            malformed = True
            break
        delete_orphaned = False
        index += 2
        if index < len(lines) and lines[index].startswith("    deleteOrphaned:"):
            if lines[index] != "    deleteOrphaned: true":
                malformed = True
                break
            delete_orphaned = True
            index += 1
        mappings.append((source_match.group(1), dest_match.group(1), delete_orphaned))

    if malformed:
        report.add(".github/sync.yml", "sync manifest does not use the approved canonical structure")
    if tuple(mappings) != APPROVED_SYNC_MAPPINGS:
        report.add(".github/sync.yml", "sync manifest mappings differ from the approved allowlist")
    report.sync_mapping_count = mappings.count(("blogs/", "blogs/", True))
    if report.sync_mapping_count != 1:
        report.add(".github/sync.yml", "exactly one blogs root-to-root mapping is required")


def _normalize_media_origins(raw_origins: Sequence[str], report: ValidationReport) -> tuple[str, ...]:
    normalized: list[str] = []
    for origin in raw_origins:
        candidate = origin.rstrip("/")
        try:
            parsed = urlsplit(candidate)
            hostname = parsed.hostname.lower() if parsed.hostname else ""
            port = parsed.port
        except ValueError:
            report.add("validator configuration", "media origin cannot be parsed safely")
            continue
        if (
            parsed.scheme != "https"
            or not hostname.endswith(".cloudfront.net")
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            report.add("validator configuration", "media origin must be a bare HTTPS cloudfront.net origin")
            continue
        normalized.append(f"https://{hostname}")
    return tuple(sorted(set(normalized)))


def _validate_new_html_occurrences(post: Post, baseline: Counter[HtmlOccurrence], report: ValidationReport) -> None:
    remaining = post.html_occurrences.copy()
    remaining.subtract(baseline)
    for occurrence, count in remaining.items():
        if count <= 0:
            continue
        if occurrence.kind == "comment":
            report.add(post.path, "new raw HTML comment is not allowed", "forbidden")
            continue
        allowed_attributes = NEW_HTML_ATTRIBUTES.get(occurrence.tag)
        if allowed_attributes is None:
            report.add(post.path, "new migrated-only raw HTML tag is not allowed", "forbidden")
            continue
        if occurrence.kind == "end":
            if occurrence.tag in VOID_TAGS:
                report.add(post.path, "new raw HTML closing tag is not allowed", "forbidden")
            continue
        names = [name for name, _ in occurrence.attributes]
        if len(names) != len(set(names)) or any(
            name.startswith("on") or name not in allowed_attributes for name in names
        ):
            report.add(
                post.path,
                "new migrated-only raw HTML attribute or value is not allowed",
                "forbidden",
            )


def _validate_post_history(post: Post, base_post: Post, report: ValidationReport) -> None:
    base_state = base_post.values.get("publicationState")
    current_state = post.values.get("publicationState")
    if base_state == "published" and current_state != "published":
        report.add(post.path, "published post cannot transition to draft or sent")
    if base_state == "sent" and current_state != "sent":
        report.add(post.path, "historical sent state is immutable")
    if post.values.get("migratedFromGhost") != base_post.values.get("migratedFromGhost"):
        report.add(post.path, "migratedFromGhost is immutable")
    newsletter_changed = post.values.get("sendNewsletter") != base_post.values.get("sendNewsletter")
    draft_promotion = (
        base_post.values.get("migratedFromGhost") is False
        and base_state == "draft"
        and base_post.values.get("sendNewsletter") is False
        and current_state == "published"
        and post.values.get("sendNewsletter") is True
    )
    if newsletter_changed and not draft_promotion:
        report.add(post.path, "historical sendNewsletter value is immutable")


def validate_repository(
    root: Path,
    *,
    base_root: Path | None = None,
    base_ref: str | None = None,
    bootstrap: bool = False,
    pre_apply: bool = False,
    media_origins: Sequence[str] = (),
    guard_only: bool = False,
) -> ValidationReport:
    root = root.resolve()
    base_root = base_root.resolve() if base_root is not None else None
    report = ValidationReport()
    contract = _load_content_contract(root, report)
    contract_origins = contract.get("approvedMediaOrigins")
    raw_origins = contract_origins if isinstance(contract_origins, list) else []
    normalized_origins = _normalize_media_origins(raw_origins, report)
    if media_origins:
        report.add(
            "validator configuration",
            "media origins must come from the committed blog content contract",
        )
    if pre_apply:
        if not bootstrap:
            report.add(
                "validator configuration",
                "pre-apply media mode is allowed only for the frozen bootstrap",
            )
        if contract.get("bootstrapComplete") is not False or normalized_origins:
            report.add(
                CONTRACT_PATH,
                "pre-apply mode requires an incomplete bootstrap and no approved origin",
            )
    elif contract.get("bootstrapComplete") is not True or len(normalized_origins) != 1:
        report.add(
            CONTRACT_PATH,
            "strict validation requires bootstrapComplete and exactly one approved CloudFront origin",
        )

    _validate_sync_mapping(root, report)
    _validate_guard_integrity(root, base_root, contract, bootstrap, report)
    if guard_only:
        return report

    post_paths, media_by_slug = _walk_repository(root, report)
    report.media_file_count = sum(len(paths) for paths in media_by_slug.values())
    files = {path.relative_to(root).as_posix(): path.read_bytes() for path in post_paths}
    posts = _parse_posts_from_bytes(files, report)

    if base_root is not None:
        base_posts = _load_base_from_directory(base_root, report)
    elif base_ref is not None:
        base_posts = _load_base_from_git(root, base_ref, report)
    else:
        base_posts = {}

    if bootstrap:
        if base_posts:
            report.add(
                "validator configuration",
                "bootstrap mode is disabled once a base blog corpus exists",
            )
        authenticated = _authenticate_bootstrap_files(root, files, contract, report)
        if authenticated:
            authenticated_report = ValidationReport()
            base_posts = _parse_posts_from_bytes(files, authenticated_report)
            if authenticated_report.errors:
                report.add("blogs/", "authenticated bootstrap corpus cannot be parsed safely")

    seen_slugs: dict[str, str] = {}
    pairs: set[tuple[str, str]] = set()
    for filename_slug, post in sorted(posts.items()):
        slug = post.values.get("slug")
        slug_key = slug.casefold() if isinstance(slug, str) else filename_slug.casefold()
        previous = seen_slugs.get(slug_key)
        if previous is not None:
            report.add(post.path, "case-insensitive duplicate slug exists")
        else:
            seen_slugs[slug_key] = post.path
        if isinstance(slug, str):
            pairs.add((filename_slug.casefold(), slug.casefold()))
        is_new = filename_slug not in base_posts
        _validate_frontmatter_semantics(post, is_new, bootstrap, report)
        _analyze_content(post, report)

    for filename_slug, base_post in base_posts.items():
        if base_post.values.get("publicationState") == "published" and filename_slug not in posts:
            report.add(base_post.path, "existing published post was deleted or renamed")

    for filename_slug, post in sorted(posts.items()):
        base_post = base_posts.get(filename_slug)
        baseline_urls: Counter[tuple[str, bool]] = Counter()
        baseline_templates: Counter[str] = Counter()
        baseline_html: Counter[HtmlOccurrence] = Counter()
        if base_post is not None:
            _analyze_content(base_post, ValidationReport())
            baseline_urls.update((reference.url, reference.is_media) for reference in base_post.urls)
            baseline_templates.update(base_post.template_tokens)
            baseline_html.update(base_post.html_occurrences)
            _validate_post_history(post, base_post, report)
        for token in post.template_tokens:
            if baseline_templates[token] > 0:
                baseline_templates[token] -= 1
            else:
                report.add(post.path, "new template token is not allowed", "forbidden")
        if post.migrated:
            _validate_new_html_occurrences(post, baseline_html, report)
        _validate_urls(post, baseline_urls, bootstrap, normalized_origins, pre_apply, report)
        feature_image = post.values.get("featureImage")
        if isinstance(feature_image, str) and feature_image.startswith("./media/"):
            post.local_media_references.add(feature_image)

    post_slugs = set(posts)
    for media_slug, media_files in sorted(media_by_slug.items()):
        if media_slug not in post_slugs:
            report.add(f"blogs/media/{media_slug}", "media directory has no owning post", "orphan")
        if len(media_files) > MAX_MEDIA_FILES:
            report.add(
                f"blogs/media/{media_slug}",
                "media directory exceeds the file-count limit",
                "media",
            )
        total_bytes = sum(path.stat().st_size for path in media_files)
        if total_bytes > MAX_MEDIA_DIRECTORY_BYTES:
            report.add(
                f"blogs/media/{media_slug}",
                "media directory exceeds the aggregate byte limit",
                "media",
            )
        owning_post = posts.get(media_slug)
        referenced_paths = owning_post.local_media_references if owning_post else set()
        for media_path in media_files:
            relative = media_path.relative_to(root).as_posix()
            expected_reference = f"./media/{media_slug}/{media_path.name}"
            if expected_reference not in referenced_paths:
                report.add(relative, "media file is not referenced by its owning post", "orphan")
            _validate_media_file(media_path, relative, report)

    for post in posts.values():
        for reference in sorted(post.local_media_references):
            media_path = root / "blogs" / reference.removeprefix("./")
            if not media_path.is_file() or media_path.is_symlink():
                report.add(
                    post.path,
                    "local media reference does not resolve to a regular file",
                    "media",
                )

    report.post_count = len(posts)
    report.state_counts.update(
        state for post in posts.values() if isinstance((state := post.values.get("publicationState")), str)
    )
    report.unique_pairs = len(pairs)
    published_slugs = _load_published_slug_ledger(root, contract, report)
    _validate_published_slug_history(posts, published_slugs, base_root, contract, report)

    if bootstrap:
        slug_set_bytes = ("\n".join(sorted(posts)) + "\n").encode("utf-8")
        if len(posts) != BOOTSTRAP_POST_COUNT:
            report.add("blogs/", "bootstrap must contain exactly 277 posts")
        if hashlib.sha256(slug_set_bytes).hexdigest() != BOOTSTRAP_SLUG_SET_SHA256:
            report.add("blogs/", "bootstrap slug set does not match the frozen corpus")
        if report.state_counts != BOOTSTRAP_STATE_COUNTS:
            report.add(
                "blogs/",
                "bootstrap publication-state counts do not match the frozen corpus",
            )
        if any(not post.migrated for post in posts.values()):
            report.add("blogs/", "every bootstrap post must be marked migratedFromGhost")
        if any(post.values.get("sendNewsletter") is not False for post in posts.values()):
            report.add(
                "blogs/",
                "every bootstrap post must suppress historical newsletter sends",
            )

    return report


def _print_report(report: ValidationReport) -> None:
    status = "passed" if report.ok else "failed"
    print(f"Blog content validation {status}")
    print(f"valid_posts={report.post_count if report.ok else 0}")
    print(
        "publication_states="
        f"published:{report.state_counts['published']},sent:{report.state_counts['sent']},draft:{report.state_counts['draft']}"
    )
    print(f"unique_filename_slug_pairs={report.unique_pairs}")
    print(f"forbidden_markdown_html={report.error_categories['forbidden']}")
    print(f"invalid_links={report.error_categories['link']}")
    print(f"unowned_orphaned_media={report.error_categories['orphan']}")
    print(f"invalid_media={report.error_categories['media']}")
    print(f"secret_findings={report.error_categories['secret']}")
    print(f"sync_mapping=blogs/->blogs/ deleteOrphaned=true count:{report.sync_mapping_count}")
    print(f"media_files={report.media_file_count}")
    if report.errors:
        print(f"errors={len(report.errors)}")
        for error in report.errors:
            print(f"ERROR {error}")


def _resolve_default_base_ref(root: Path) -> str | None:
    for candidate in ("origin/main", "main"):
        exists = subprocess.run(
            ["git", "rev-parse", "--verify", candidate],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if exists.returncode != 0:
            continue
        merge_base = subprocess.run(
            ["git", "merge-base", "HEAD", candidate],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        if merge_base.returncode == 0 and merge_base.stdout.strip():
            return merge_base.stdout.strip()
    return None


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root (default: current directory)")
    parser.add_argument("--base-ref", help="Git revision used to protect historical blog state")
    parser.add_argument("--base-root", type=Path, help="trusted base checkout used by required CI")
    parser.add_argument("--bootstrap", action="store_true", help="authenticate the initial frozen corpus")
    parser.add_argument("--guard-only", action="store_true", help="validate guard integrity without rescanning posts")
    parser.add_argument(
        "--pre-apply",
        action="store_true",
        help="temporarily admit the exact https://DOMAIN/media/blog/ placeholder before the operator-approved CDN apply",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _build_argument_parser().parse_args(argv)
    if arguments.base_ref and arguments.base_root:
        raise SystemExit("--base-ref and --base-root are mutually exclusive")
    base_ref = arguments.base_ref
    if base_ref is None and arguments.base_root is None:
        base_ref = _resolve_default_base_ref(arguments.root.resolve())
    report = validate_repository(
        arguments.root,
        base_ref=base_ref,
        base_root=arguments.base_root,
        bootstrap=arguments.bootstrap,
        pre_apply=arguments.pre_apply,
        guard_only=arguments.guard_only,
    )
    _print_report(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
