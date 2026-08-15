from __future__ import annotations

import json
import os
import shutil
import stat
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from typing import Callable

from scripts import validate_blog_content as validator


TIMESTAMP = "2026-08-12T12:00:00.000Z"


def _chunk(chunk_type: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", checksum)


def png_bytes(width: int = 1, height: int = 1, *, animated: bool = False) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    chunks = [_chunk(b"IHDR", header)]
    if animated:
        chunks.append(_chunk(b"acTL", struct.pack(">II", 1, 0)))
    chunks.extend((_chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00")), _chunk(b"IEND", b"")))
    return b"\x89PNG\r\n\x1a\n" + b"".join(chunks)


def animated_webp_bytes() -> bytes:
    payload = bytes([0x02, 0, 0, 0, 0, 0, 0, 0, 0, 0])
    chunk = b"VP8X" + struct.pack("<I", len(payload)) + payload
    riff_size = 4 + len(chunk)
    return b"RIFF" + struct.pack("<I", riff_size) + b"WEBP" + chunk


def gif_bytes(width: int, height: int, frame_count: int) -> bytes:
    data = bytearray(b"GIF89a")
    data.extend(struct.pack("<HHBBB", width, height, 0, 0, 0))
    for _ in range(frame_count):
        data.extend(b"\x2c")
        data.extend(struct.pack("<HHHHB", 0, 0, width, height, 0))
        data.extend(b"\x02\x02\x4c\x01\x00")
    data.extend(b"\x3b")
    return bytes(data)


def canonical_values(slug: str, *, published: bool = False) -> dict[str, object]:
    return {
        "title": f"Post {slug}",
        "description": "A useful description." if published else None,
        "excerpt": "A useful excerpt.",
        "slug": slug,
        "publicationState": "published" if published else "draft",
        "publishedAt": TIMESTAMP if published else None,
        "updatedAt": TIMESTAMP,
        "author": "author-key",
        "tags": ["browser-automation"],
        "featureImage": None,
        "featureImageAlt": None,
        "featureImageCaption": None,
        "sendNewsletter": published,
        "migratedFromGhost": False,
    }


def write_post(
    root: Path,
    slug: str,
    *,
    filename: str | None = None,
    values: dict[str, object] | None = None,
    body: str = "Safe body.\n",
) -> Path:
    post_values = values or canonical_values(slug)
    lines = ["---"]
    lines.extend(
        f"{key}: {validator._canonical_json(value)}"  # noqa: SLF001 - contract fixture uses validator encoding
        for key, value in post_values.items()
    )
    lines.extend(("---", body.rstrip("\n"), ""))
    path = root / "blogs" / (filename or f"{slug}.md")
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    if post_values.get("publicationState") == "published":
        ledger = root / "scripts" / "blog_content_published_slugs.txt"
        existing = set(ledger.read_text(encoding="utf-8").splitlines())
        existing.add(slug)
        ledger.write_text("".join(f"{item}\n" for item in sorted(existing)), encoding="utf-8")
    return path


def make_repository() -> Path:
    root = Path(tempfile.mkdtemp(prefix="blog-validator-test-"))
    (root / "blogs").mkdir()
    (root / "blogs" / "README.md").write_text("# Blog fixtures\n", encoding="utf-8")
    (root / ".github").mkdir()
    sync_lines = ["Skyvern-AI/skyvern-cloud:"]
    for source, dest, delete_orphaned in validator.APPROVED_SYNC_MAPPINGS:
        sync_lines.extend((f"  - source: {source}", f"    dest: {dest}"))
        if delete_orphaned:
            sync_lines.append("    deleteOrphaned: true")
    (root / ".github" / "sync.yml").write_text("\n".join(sync_lines) + "\n", encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts" / "blog_content_contract.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "bootstrapComplete": True,
                "approvedMediaOrigins": ["https://example.cloudfront.net"],
                "bootstrapHashLedger": "scripts/blog_content_bootstrap_hashes.json",
                "publishedSlugLedger": "scripts/blog_content_published_slugs.txt",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "scripts" / "blog_content_bootstrap_hashes.json").write_text(
        '{\n  "schemaVersion": 1,\n  "posts": []\n}\n',
        encoding="utf-8",
    )
    (root / "scripts" / "blog_content_published_slugs.txt").write_text("", encoding="utf-8")
    return root


def write_guard_files(root: Path, content: str = "trusted guard\n") -> None:
    for relative_path in (
        ".github/workflows/ci.yml",
        "scripts/validate_blog_content.py",
        "scripts/tests/test_validate_blog_content.py",
    ):
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


class BlogValidatorTests(unittest.TestCase):
    roots: list[Path]

    def setUp(self) -> None:
        self.roots = []

    def tearDown(self) -> None:
        for root in self.roots:
            for path in sorted(root.rglob("*"), reverse=True):
                if path.is_symlink():
                    path.unlink()

            shutil.rmtree(root, ignore_errors=True)

    def repository(self) -> Path:
        root = make_repository()
        self.roots.append(root)
        return root

    def assert_invalid(
        self,
        configure: Callable[[Path], None],
        expected_message: str,
        *,
        base_root: Path | None = None,
    ) -> validator.ValidationReport:
        root = self.repository()
        configure(root)
        report = validator.validate_repository(root, base_root=base_root)
        self.assertFalse(report.ok)
        self.assertTrue(
            any(expected_message in error for error in report.errors),
            f"expected {expected_message!r} in {report.errors!r}",
        )
        return report

    def test_valid_new_published_post_with_owned_image_passes(self) -> None:
        root = self.repository()
        slug = "valid-post"
        values = canonical_values(slug, published=True)
        values["featureImage"] = f"./media/{slug}/cover.png"
        write_post(root, slug, values=values)
        media = root / "blogs" / "media" / slug
        media.mkdir(parents=True)
        (media / "cover.png").write_bytes(png_bytes())

        report = validator.validate_repository(root)

        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.post_count, 1)
        self.assertEqual(report.media_file_count, 1)

    def test_reference_style_owned_image_passes(self) -> None:
        root = self.repository()
        slug = "reference-image"
        write_post(
            root,
            slug,
            body=f"![Cover][cover]\n\n[cover]: ./media/{slug}/cover.png\n",
        )
        media = root / "blogs" / "media" / slug
        media.mkdir(parents=True)
        (media / "cover.png").write_bytes(png_bytes())

        report = validator.validate_repository(root)

        self.assertTrue(report.ok, report.errors)

    def test_migrated_media_url_is_not_reclassified_as_a_bare_link(self) -> None:
        base = self.repository()
        current = self.repository()
        slug = "migrated-image"
        values = canonical_values(slug)
        values["migratedFromGhost"] = True
        body = "![Cover](https://example.cloudfront.net/media/blog/frozen.png)\n"
        write_post(base, slug, values=values, body=body)
        write_post(current, slug, values=values, body=body)

        report = validator.validate_repository(current, base_root=base)

        self.assertTrue(report.ok, report.errors)

    def test_merged_draft_can_be_promoted_for_recovery_send(self) -> None:
        base = self.repository()
        current = self.repository()
        slug = "promoted-draft"
        write_post(base, slug)
        write_post(current, slug, values=canonical_values(slug, published=True))

        report = validator.validate_repository(current, base_root=base)

        self.assertTrue(report.ok, report.errors)

    def test_exact_migrated_http_slashless_link_is_preserved(self) -> None:
        base = self.repository()
        current = self.repository()
        slug = "migrated-link"
        values = canonical_values(slug)
        values["migratedFromGhost"] = True
        body = "[Legacy](http://www.skyvern.com/blog/legacy-post)\n"
        write_post(base, slug, values=values, body=body)
        write_post(current, slug, values=values, body=body)

        report = validator.validate_repository(current, base_root=base)

        self.assertTrue(report.ok, report.errors)

    def test_preserved_normal_link_cannot_be_reclassified_as_media(self) -> None:
        base = self.repository()
        current = self.repository()
        slug = "legacy-localhost"
        values = canonical_values(slug)
        values["migratedFromGhost"] = True
        write_post(base, slug, values=values, body="[localhost](http://localhost)\n")
        write_post(current, slug, values=values, body="![localhost](http://localhost)\n")

        report = validator.validate_repository(current, base_root=base)

        self.assertFalse(report.ok)
        self.assertTrue(
            any("local or private network URL" in error for error in report.errors),
            report.errors,
        )

    def test_new_copy_of_migrated_http_link_fails(self) -> None:
        base = self.repository()
        slug = "migrated-link-copy"
        values = canonical_values(slug)
        values["migratedFromGhost"] = True
        body = "[Legacy](http://www.skyvern.com/blog/legacy-post)\n"
        write_post(base, slug, values=values, body=body)

        def configure(root: Path) -> None:
            write_post(root, slug, values=values, body=body + body)

        self.assert_invalid(configure, "new HTTP URL is not allowed", base_root=base)

    def test_duplicate_slug_fails(self) -> None:
        def configure(root: Path) -> None:
            values = canonical_values("duplicate")
            write_post(root, "duplicate", filename="first.md", values=values)
            write_post(root, "duplicate", filename="second.md", values=values)

        self.assert_invalid(configure, "case-insensitive duplicate slug")

    def test_wrong_filename_fails(self) -> None:
        self.assert_invalid(
            lambda root: write_post(root, "right-slug", filename="wrong-name.md"),
            "slug does not match the filename",
        )

    def test_raw_script_tag_fails(self) -> None:
        self.assert_invalid(
            lambda root: write_post(root, "script-post", body="<script>alert('x')</script>\n"),
            "raw HTML tag is not allowed",
        )

    def test_invalid_code_delimiters_do_not_hide_script_tags(self) -> None:
        cases = (
            (
                "invalid-fence",
                '```lang`\n\n<script src="https://attacker.example/fence.js"></script>\n',
            ),
            (
                "invalid-code-span",
                '`visible text\n\n<script src="https://attacker.example/span.js"></script>\n``\n',
            ),
        )
        for slug, body in cases:
            with self.subTest(slug=slug):
                self.assert_invalid(
                    lambda root, slug=slug, body=body: write_post(root, slug, body=body),
                    "raw HTML tag is not allowed",
                )

    def test_deep_blockquote_cannot_hide_script_tag(self) -> None:
        self.assert_invalid(
            lambda root: write_post(
                root,
                "deep-script",
                body="> " * 21 + "<script>alert(document.domain)</script>\n",
            ),
            "raw HTML tag is not allowed",
        )

    def test_gfm_footnote_cannot_hide_raw_iframe(self) -> None:
        self.assert_invalid(
            lambda root: write_post(
                root,
                "footnote-iframe",
                body=(
                    "See note[^embed]\n\n"
                    "[^embed]: /blog/\n\n"
                    '    <iframe src="https://attacker.example/embed"></iframe>\n'
                ),
            ),
            "raw HTML tag is not allowed",
        )

    def test_noncanonical_gfm_footnote_labels_fail_closed(self) -> None:
        cases = (
            (
                "mixed-case-footnote",
                'See note[^EMBED]\n\n[^embed]: /blog/\n\n    <iframe src="https://attacker.example/embed"></iframe>\n',
            ),
            (
                "escaped-footnote",
                'See note[^a\\]b]\n\n[^a\\]b]: /blog/\n\n    <iframe src="https://attacker.example/embed"></iframe>\n',
            ),
        )
        for slug, body in cases:
            with self.subTest(slug=slug):
                self.assert_invalid(
                    lambda root, slug=slug, body=body: write_post(root, slug, body=body),
                    "footnote label is not canonical lowercase ASCII",
                )

    def test_duplicate_gfm_footnote_definitions_fail_closed(self) -> None:
        cases = (
            (
                "duplicate-footnote",
                "See note[^embed]\n\n"
                "[^embed]: /blog/\n\n"
                '    <iframe src="https://attacker.example/embed"></iframe>\n\n'
                "[^embed]: Safe replacement.\n",
            ),
            (
                "duplicate-blockquote-footnote",
                "> See note[^embed]\n>\n"
                "> [^embed]: /blog/\n>\n"
                '>     <iframe src="https://attacker.example/embed"></iframe>\n>\n'
                "> [^embed]: Safe replacement.\n",
            ),
        )
        for slug, body in cases:
            with self.subTest(slug=slug):
                self.assert_invalid(
                    lambda root, slug=slug, body=body: write_post(root, slug, body=body),
                    "duplicate footnote definition is not allowed",
                )

    def test_mdx_esm_forms_fail(self) -> None:
        cases = (
            ("named-export", "export { foo }\n"),
            ("star-export", "export * from './module.js'\n"),
            (
                "multiline-import",
                "import {\n  foo,\n  bar,\n} from './module.js'\n",
            ),
        )
        for slug, body in cases:
            with self.subTest(slug=slug):
                self.assert_invalid(
                    lambda root, slug=slug, body=body: write_post(root, slug, body=body),
                    "MDX import or export is not allowed",
                )

    def test_javascript_url_fails(self) -> None:
        self.assert_invalid(
            lambda root: write_post(root, "active-url", body="[bad](javascript:alert(1))\n"),
            "active or local URL scheme",
        )

    def test_missing_internal_blog_trailing_slash_fails(self) -> None:
        self.assert_invalid(
            lambda root: write_post(root, "slashless", body="[bad](/blog/another-post)\n"),
            "missing its trailing slash",
        )

    def test_unknown_frontmatter_key_fails(self) -> None:
        def configure(root: Path) -> None:
            values = canonical_values("unknown-key")
            values["privateRouteId"] = "not-public"
            write_post(root, "unknown-key", values=values)

        self.assert_invalid(configure, "not in the public schema")

    def test_oversized_image_fails(self) -> None:
        def configure(root: Path) -> None:
            slug = "large-image"
            values = canonical_values(slug)
            values["featureImage"] = f"./media/{slug}/large.png"
            write_post(root, slug, values=values)
            media = root / "blogs" / "media" / slug
            media.mkdir(parents=True)
            image = png_bytes()
            (media / "large.png").write_bytes(image + b"x" * (validator.MAX_MEDIA_FILE_BYTES + 1 - len(image)))

        self.assert_invalid(configure, "exceeds the encoded byte limit")

    def test_twenty_first_image_fails(self) -> None:
        def configure(root: Path) -> None:
            slug = "too-many-images"
            body = "\n".join(f"![{index}](./media/{slug}/{index}.png)" for index in range(21))
            write_post(root, slug, body=f"{body}\n")
            media = root / "blogs" / "media" / slug
            media.mkdir(parents=True)
            for index in range(21):
                (media / f"{index}.png").write_bytes(png_bytes())

        self.assert_invalid(configure, "exceeds the file-count limit")

    def test_aggregate_image_size_overflow_fails(self) -> None:
        def configure(root: Path) -> None:
            slug = "aggregate-overflow"
            body = "\n".join(f"![{index}](./media/{slug}/{index}.png)" for index in range(6))
            write_post(root, slug, body=f"{body}\n")
            media = root / "blogs" / "media" / slug
            media.mkdir(parents=True)
            image = png_bytes()
            padded = image + b"x" * (validator.MAX_MEDIA_FILE_BYTES - len(image))
            for index in range(6):
                (media / f"{index}.png").write_bytes(padded)

        self.assert_invalid(configure, "exceeds the aggregate byte limit")

    def test_orphan_image_fails(self) -> None:
        def configure(root: Path) -> None:
            slug = "orphan-image"
            write_post(root, slug)
            media = root / "blogs" / "media" / slug
            media.mkdir(parents=True)
            (media / "orphan.png").write_bytes(png_bytes())

        self.assert_invalid(configure, "not referenced by its owning post")

    def test_media_symlink_fails(self) -> None:
        def configure(root: Path) -> None:
            slug = "symlink-image"
            values = canonical_values(slug)
            values["featureImage"] = f"./media/{slug}/linked.png"
            write_post(root, slug, values=values)
            media = root / "blogs" / "media" / slug
            media.mkdir(parents=True)
            target = root / "outside.png"
            target.write_bytes(png_bytes())
            os.symlink(target, media / "linked.png")

        self.assert_invalid(configure, "media symlink is not allowed")

    def test_new_published_post_without_send_fails(self) -> None:
        def configure(root: Path) -> None:
            values = canonical_values("no-send", published=True)
            values["sendNewsletter"] = False
            write_post(root, "no-send", values=values)

        self.assert_invalid(configure, "must set sendNewsletter true")

    def test_published_post_deletion_fails(self) -> None:
        base = self.repository()
        write_post(base, "existing-published", values=canonical_values("existing-published", published=True))

        self.assert_invalid(
            lambda root: write_post(root, "remaining-draft"),
            "existing published post was deleted or renamed",
            base_root=base,
        )

    def test_published_post_cannot_be_downgraded(self) -> None:
        base = self.repository()
        current = self.repository()
        slug = "published-history"
        write_post(base, slug, values=canonical_values(slug, published=True))
        write_post(current, slug)

        report = validator.validate_repository(current, base_root=base)

        self.assertTrue(any("published post cannot transition" in error for error in report.errors), report.errors)

    def test_migrated_flag_is_immutable(self) -> None:
        base = self.repository()
        current = self.repository()
        slug = "migration-history"
        write_post(base, slug)
        values = canonical_values(slug)
        values["migratedFromGhost"] = True
        write_post(current, slug, values=values)

        report = validator.validate_repository(current, base_root=base)

        self.assertTrue(any("migratedFromGhost is immutable" in error for error in report.errors), report.errors)

    def test_historical_newsletter_flag_is_immutable(self) -> None:
        base = self.repository()
        current = self.repository()
        slug = "newsletter-history"
        values = canonical_values(slug, published=True)
        values["migratedFromGhost"] = True
        values["sendNewsletter"] = False
        write_post(base, slug, values=values)
        changed_values = values.copy()
        changed_values["sendNewsletter"] = True
        write_post(current, slug, values=changed_values)

        report = validator.validate_repository(current, base_root=base)

        self.assertTrue(
            any("historical sendNewsletter value is immutable" in error for error in report.errors),
            report.errors,
        )

    def test_new_migrated_only_html_occurrence_fails(self) -> None:
        base = self.repository()
        slug = "migrated-html"
        values = canonical_values(slug)
        values["migratedFromGhost"] = True
        write_post(base, slug, values=values, body="<p>Preserved</p>\n")

        self.assert_invalid(
            lambda root: write_post(
                root,
                slug,
                values=values,
                body='<p>Preserved</p>\n<p style="position:fixed">New overlay</p>\n',
            ),
            "new migrated-only raw HTML attribute or value",
            base_root=base,
        )

    def test_duplicate_frontmatter_key_fails(self) -> None:
        def configure(root: Path) -> None:
            path = write_post(root, "duplicate-key")
            text = path.read_text(encoding="utf-8")
            path.write_text(text.replace('title: "Post duplicate-key"', 'title: "Post duplicate-key"\ntitle: "Again"'))

        self.assert_invalid(configure, "frontmatter key is duplicated")

    def test_wrong_frontmatter_type_fails(self) -> None:
        def configure(root: Path) -> None:
            values = canonical_values("wrong-type")
            values["tags"] = "browser-automation"
            write_post(root, "wrong-type", values=values)

        self.assert_invalid(configure, "tags must be an array")

    def test_bom_fails(self) -> None:
        def configure(root: Path) -> None:
            path = write_post(root, "bom")
            path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())

        self.assert_invalid(configure, "byte-order mark")

    def test_nul_fails(self) -> None:
        def configure(root: Path) -> None:
            path = write_post(root, "nul")
            path.write_bytes(path.read_bytes() + b"\x00")

        self.assert_invalid(configure, "NUL byte")

    def test_cr_newline_fails(self) -> None:
        def configure(root: Path) -> None:
            path = write_post(root, "cr-newline")
            path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))

        self.assert_invalid(configure, "non-LF newline")

    def test_malformed_frontmatter_delimiter_fails(self) -> None:
        def configure(root: Path) -> None:
            path = write_post(root, "bad-delimiter")
            path.write_text(path.read_text(encoding="utf-8").replace("---\n", "--\n", 1), encoding="utf-8")

        self.assert_invalid(configure, "opening delimiter is malformed")

    def test_noncanonical_frontmatter_encoding_fails(self) -> None:
        def configure(root: Path) -> None:
            path = write_post(root, "noncanonical")
            text = path.read_text(encoding="utf-8")
            path.write_text(
                text.replace('tags: ["browser-automation"]', 'tags: [ "browser-automation" ]'),
                encoding="utf-8",
            )

        self.assert_invalid(configure, "value encoding is not canonical")

    def test_missing_required_frontmatter_fails(self) -> None:
        def configure(root: Path) -> None:
            path = write_post(root, "missing-required")
            text = path.read_text(encoding="utf-8")
            path.write_text(
                "\n".join(line for line in text.splitlines() if not line.startswith("excerpt:")) + "\n",
                encoding="utf-8",
            )

        self.assert_invalid(configure, "required frontmatter key is missing")

    def test_invalid_state_timestamp_combination_fails(self) -> None:
        def configure(root: Path) -> None:
            values = canonical_values("missing-published-at", published=True)
            values["publishedAt"] = None
            write_post(root, "missing-published-at", values=values)

        self.assert_invalid(configure, "requires publishedAt")

    def test_impossible_utc_timestamp_fails(self) -> None:
        def configure(root: Path) -> None:
            values = canonical_values("impossible-timestamp", published=True)
            values["publishedAt"] = "2024-99-31T12:00:17.000Z"
            write_post(root, "impossible-timestamp", values=values)

        self.assert_invalid(configure, "timestamp is not an ISO-8601 UTC string")

    def test_post_byte_cap_fails(self) -> None:
        def configure(root: Path) -> None:
            path = write_post(root, "large-post")
            path.write_bytes(path.read_bytes() + b"x" * validator.MAX_POST_BYTES)

        self.assert_invalid(configure, "post exceeds the byte limit")

    def test_secret_material_fails(self) -> None:
        self.assert_invalid(
            lambda root: write_post(
                root,
                "secret",
                body="-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----\n",
            ),
            "possible secret material",
        )

    def test_posthog_personal_api_key_fails(self) -> None:
        self.assert_invalid(
            lambda root: write_post(
                root,
                "posthog-secret",
                body="Accidentally pasted phc_abcdefghijklmnopqrstuvwxyz0123456789\n",
            ),
            "possible secret material",
        )

    def test_aws_temporary_access_key_fails(self) -> None:
        self.assert_invalid(
            lambda root: write_post(
                root,
                "aws-sts-secret",
                body="Accidentally pasted ASIAABCDEFGHIJKLMNOP\n",
            ),
            "possible secret material",
        )

    def test_new_post_cannot_claim_migrated_state(self) -> None:
        def configure(root: Path) -> None:
            values = canonical_values("fake-migration")
            values["migratedFromGhost"] = True
            write_post(root, "fake-migration", values=values)

        self.assert_invalid(configure, "cannot claim migratedFromGhost")

    def test_reserved_slug_fails(self) -> None:
        self.assert_invalid(lambda root: write_post(root, "sitemap-posts-extra"), "invalid or reserved")

    def test_new_sent_state_fails(self) -> None:
        def configure(root: Path) -> None:
            values = canonical_values("new-sent")
            values["publicationState"] = "sent"
            values["publishedAt"] = TIMESTAMP
            write_post(root, "new-sent", values=values)

        self.assert_invalid(configure, "new sent post is not allowed")

    def test_static_dimension_cap_fails(self) -> None:
        def configure(root: Path) -> None:
            slug = "huge-dimensions"
            values = canonical_values(slug)
            values["featureImage"] = f"./media/{slug}/huge.png"
            write_post(root, slug, values=values)
            media = root / "blogs" / "media" / slug
            media.mkdir(parents=True)
            (media / "huge.png").write_bytes(png_bytes(8_193, 1))

        self.assert_invalid(configure, "dimension or decoded-pixel budget")

    def test_static_decoded_pixel_cap_fails(self) -> None:
        def configure(root: Path) -> None:
            slug = "too-many-pixels"
            values = canonical_values(slug)
            values["featureImage"] = f"./media/{slug}/huge.png"
            write_post(root, slug, values=values)
            media = root / "blogs" / "media" / slug
            media.mkdir(parents=True)
            (media / "huge.png").write_bytes(png_bytes(8_000, 5_000))

        self.assert_invalid(configure, "dimension or decoded-pixel budget")

    def test_gif_frame_count_cap_fails(self) -> None:
        def configure(root: Path) -> None:
            slug = "too-many-frames"
            values = canonical_values(slug)
            values["featureImage"] = f"./media/{slug}/animated.gif"
            write_post(root, slug, values=values)
            media = root / "blogs" / "media" / slug
            media.mkdir(parents=True)
            (media / "animated.gif").write_bytes(gif_bytes(1, 1, 101))

        self.assert_invalid(configure, "GIF dimension, frame, or decoded-pixel budget")

    def test_gif_summed_pixel_cap_fails(self) -> None:
        def configure(root: Path) -> None:
            slug = "gif-pixel-sum"
            values = canonical_values(slug)
            values["featureImage"] = f"./media/{slug}/animated.gif"
            write_post(root, slug, values=values)
            media = root / "blogs" / "media" / slug
            media.mkdir(parents=True)
            (media / "animated.gif").write_bytes(gif_bytes(1_000, 1_000, 81))

        self.assert_invalid(configure, "GIF dimension, frame, or decoded-pixel budget")

    def test_apng_fails(self) -> None:
        def configure(root: Path) -> None:
            slug = "animated-png"
            values = canonical_values(slug)
            values["featureImage"] = f"./media/{slug}/animated.png"
            write_post(root, slug, values=values)
            media = root / "blogs" / "media" / slug
            media.mkdir(parents=True)
            (media / "animated.png").write_bytes(png_bytes(animated=True))

        self.assert_invalid(configure, "header, chunks, or dimensions are invalid")

    def test_animated_webp_fails(self) -> None:
        def configure(root: Path) -> None:
            slug = "animated-webp"
            values = canonical_values(slug)
            values["featureImage"] = f"./media/{slug}/animated.webp"
            write_post(root, slug, values=values)
            media = root / "blogs" / "media" / slug
            media.mkdir(parents=True)
            (media / "animated.webp").write_bytes(animated_webp_bytes())

        self.assert_invalid(configure, "header, chunks, or dimensions are invalid")

    def test_extension_magic_mismatch_fails(self) -> None:
        def configure(root: Path) -> None:
            slug = "magic-mismatch"
            values = canonical_values(slug)
            values["featureImage"] = f"./media/{slug}/image.jpg"
            write_post(root, slug, values=values)
            media = root / "blogs" / "media" / slug
            media.mkdir(parents=True)
            (media / "image.jpg").write_bytes(png_bytes())

        self.assert_invalid(configure, "does not match magic bytes")

    def test_executable_media_fails(self) -> None:
        def configure(root: Path) -> None:
            slug = "executable-image"
            values = canonical_values(slug)
            values["featureImage"] = f"./media/{slug}/image.png"
            write_post(root, slug, values=values)
            media = root / "blogs" / "media" / slug
            media.mkdir(parents=True)
            image = media / "image.png"
            image.write_bytes(png_bytes())
            image.chmod(image.stat().st_mode | stat.S_IXUSR)

        self.assert_invalid(configure, "executable media file")

    def test_missing_feature_image_fails(self) -> None:
        def configure(root: Path) -> None:
            values = canonical_values("missing-feature")
            values["featureImage"] = "./media/missing-feature/missing.png"
            write_post(root, "missing-feature", values=values)

        self.assert_invalid(configure, "does not resolve to a regular file")

    def test_new_post_media_hotlink_fails(self) -> None:
        def configure(root: Path) -> None:
            values = canonical_values("hotlink")
            values["featureImage"] = "https://example.com/image.png"
            write_post(root, "hotlink", values=values)

        self.assert_invalid(configure, "cannot hotlink media")

    def test_media_file_secret_material_fails(self) -> None:
        def configure(root: Path) -> None:
            slug = "media-secret"
            values = canonical_values(slug)
            values["featureImage"] = f"./media/{slug}/cover.png"
            write_post(root, slug, values=values)
            media = root / "blogs" / "media" / slug
            media.mkdir(parents=True)
            (media / "cover.png").write_bytes(png_bytes() + b"\nphc_abcdefghijklmnopqrstuvwxyz0123456789\n")

        self.assert_invalid(configure, "possible secret material")

    def test_escaped_reference_image_label_hotlink_fails(self) -> None:
        self.assert_invalid(
            lambda root: write_post(
                root,
                "reference-hotlink",
                body="![track\\]ing][pixel]\n\n[pixel]: https://example.com/pixel.png\n",
            ),
            "cannot hotlink media",
        )

    def test_nested_inline_image_label_hotlink_fails(self) -> None:
        self.assert_invalid(
            lambda root: write_post(
                root,
                "nested-image-hotlink",
                body="![outer [inner]](https://attacker.example/pixel.png)\n",
            ),
            "cannot hotlink media",
        )

    def test_nested_full_reference_image_label_hotlink_fails(self) -> None:
        self.assert_invalid(
            lambda root: write_post(
                root,
                "nested-full-reference-hotlink",
                body="![outer [inner]][pixel]\n\n[pixel]: https://attacker.example/pixel.png\n",
            ),
            "cannot hotlink media",
        )

    def test_collapsed_reference_image_hotlink_fails(self) -> None:
        self.assert_invalid(
            lambda root: write_post(
                root,
                "collapsed-reference-hotlink",
                body="![pixel][]\n\n[pixel]: https://attacker.example/collapsed-pixel.png\n",
            ),
            "cannot hotlink media",
        )

    def test_shortcut_reference_image_hotlink_fails(self) -> None:
        self.assert_invalid(
            lambda root: write_post(
                root,
                "shortcut-reference-hotlink",
                body="![pixel]\n\n[pixel]: https://attacker.example/shortcut-pixel.png\n",
            ),
            "cannot hotlink media",
        )

    def test_lazy_destination_reference_image_hotlink_fails(self) -> None:
        self.assert_invalid(
            lambda root: write_post(
                root,
                "lazy-reference-hotlink",
                body="![pixel][r]\n\n[r]:\n  https://attacker.example/pixel.png\n",
            ),
            "cannot hotlink media",
        )

    def test_gfm_table_cells_cannot_hide_image_hotlinks(self) -> None:
        self.assert_invalid(
            lambda root: write_post(
                root,
                "table-cell-hotlink",
                body=("| A | B | C |\n| --- | --- | --- |\n| ` | ![external](https://attacker.example/p.png) | ` |\n"),
            ),
            "cannot hotlink media",
        )

    def test_template_tokens_in_rendered_surfaces_fail(self) -> None:
        token = "{{{RESEND_UNSUBSCRIBE_URL}}}"

        def frontmatter(root: Path) -> None:
            values = canonical_values("frontmatter-template")
            values["title"] = token
            write_post(root, "frontmatter-template", values=values)

        cases: tuple[tuple[str, Callable[[Path], None]], ...] = (
            (
                "inline-code",
                lambda root: write_post(root, "inline-template", body=f"`{token}`\n"),
            ),
            (
                "fenced-code",
                lambda root: write_post(root, "fenced-template", body=f"```\n{token}\n```\n"),
            ),
            (
                "image-label",
                lambda root: write_post(
                    root,
                    "image-label-template",
                    body=f"![{token}](https://example.com/pixel.png)\n",
                ),
            ),
            (
                "formatted-text",
                lambda root: write_post(
                    root,
                    "formatted-template",
                    body="{{{**RESEND_UNSUBSCRIBE_URL**}}}\n",
                ),
            ),
            (
                "link-title",
                lambda root: write_post(
                    root,
                    "link-title-template",
                    body=f'[link](https://example.com/ "{token}")\n',
                ),
            ),
            (
                "image-title",
                lambda root: write_post(
                    root,
                    "image-title-template",
                    body=f'![pixel](https://example.com/pixel.png "{token}")\n',
                ),
            ),
            (
                "raw-html-entity",
                lambda root: write_post(
                    root,
                    "html-entity-template",
                    body="<p>&#123;&#123;&#123;RESEND_UNSUBSCRIBE_URL&#125;&#125;&#125;</p>\n",
                ),
            ),
            ("frontmatter", frontmatter),
        )
        for name, configure in cases:
            with self.subTest(name=name):
                self.assert_invalid(configure, "new template token is not allowed")

    def test_protocol_relative_url_fails(self) -> None:
        self.assert_invalid(
            lambda root: write_post(root, "protocol-relative", body="[bad](//example.com/path)\n"),
            "protocol-relative URL",
        )

    def test_private_network_url_fails(self) -> None:
        self.assert_invalid(
            lambda root: write_post(root, "private-url", body="[bad](http://127.0.0.1/path)\n"),
            "local or private network URL",
        )

    def test_bare_gfm_ghost_url_fails(self) -> None:
        self.assert_invalid(
            lambda root: write_post(
                root,
                "bare-ghost-url",
                body="See https://skyvern.ghost.io/content/images/private.png\n",
            ),
            "Ghost origin or storage URL",
        )

    def test_whatwg_numeric_private_network_urls_fail(self) -> None:
        for index, url in enumerate(("http://2130706433/admin", "http://127.1/admin", "http://0x7f000001/admin")):
            with self.subTest(url=url):
                self.assert_invalid(
                    lambda root, value=url, slug=f"numeric-private-{index}": write_post(
                        root,
                        slug,
                        body=f"See {value}\n",
                    ),
                    "local or private network URL",
                )

    def test_idna_localhost_url_fails(self) -> None:
        self.assert_invalid(
            lambda root: write_post(root, "idna-localhost", body="See https://ⓛⓞⓒⓐⓛⓗⓞⓢⓣ/admin\n"),
            "local or private network URL",
        )

    def test_wrong_sync_mapping_fails(self) -> None:
        def configure(root: Path) -> None:
            write_post(root, "wrong-sync")
            path = root / ".github" / "sync.yml"
            path.write_text(
                path.read_text(encoding="utf-8").replace("    dest: blogs/\n", "    dest: landing_page/blogs/\n"),
                encoding="utf-8",
            )

        self.assert_invalid(configure, "sync manifest mappings differ from the approved allowlist")

    def test_extra_sync_mapping_fails(self) -> None:
        def configure(root: Path) -> None:
            write_post(root, "extra-sync")
            path = root / ".github" / "sync.yml"
            with path.open("a", encoding="utf-8") as sync_file:
                sync_file.write("  - source: .github/workflows/\n    dest: .github/workflows/\n")

        self.assert_invalid(configure, "sync manifest mappings differ from the approved allowlist")

    def test_changed_guard_file_fails_against_trusted_base(self) -> None:
        base = self.repository()
        current = self.repository()
        write_guard_files(base)
        write_guard_files(current)
        (current / "scripts" / "validate_blog_content.py").write_text("raise SystemExit(0)\n", encoding="utf-8")

        report = validator.validate_repository(current, base_root=base, guard_only=True)

        self.assertTrue(any("protected blog guard differs" in error for error in report.errors), report.errors)

    def test_changed_sync_manifest_fails_against_trusted_base(self) -> None:
        base = self.repository()
        current = self.repository()
        write_guard_files(base)
        write_guard_files(current)
        sync_path = current / ".github" / "sync.yml"
        sync_path.write_text(sync_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

        report = validator.validate_repository(current, base_root=base, guard_only=True)

        self.assertTrue(
            any(error.startswith(".github/sync.yml: protected blog guard differs") for error in report.errors),
            report.errors,
        )

    def test_completed_contract_is_immutable(self) -> None:
        base = self.repository()
        current = self.repository()
        write_guard_files(base)
        write_guard_files(current)
        contract_path = current / "scripts" / "blog_content_contract.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        contract["approvedMediaOrigins"] = ["https://other.cloudfront.net"]
        contract_path.write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")

        report = validator.validate_repository(current, base_root=base, guard_only=True)

        self.assertTrue(any("completed blog content contract is immutable" in error for error in report.errors))

    def test_bootstrap_hash_mismatch_fails(self) -> None:
        root = self.repository()
        post = write_post(root, "bootstrap-mismatch")
        contract_path = root / "scripts" / "blog_content_contract.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        contract["bootstrapComplete"] = False
        contract["approvedMediaOrigins"] = []
        contract_path.write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")
        (root / "scripts" / "blog_content_bootstrap_hashes.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "posts": [
                        {
                            "slug": "bootstrap-mismatch",
                            "path": "blogs/bootstrap-mismatch.md",
                            "sha256": "0" * 64,
                        }
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        report = validator.validate_repository(root, bootstrap=True, pre_apply=True)

        self.assertTrue(post.is_file())
        self.assertTrue(any("frozen bootstrap hash ledger" in error for error in report.errors), report.errors)


if __name__ == "__main__":
    unittest.main()
