"""Every ArtifactType must be deliberately placed in each type-keyed site, so adding a value cannot
silently skip a map: the storage extension map (KeyError on first write), secret redaction for text,
the screenshot prefix (step-archive filename collisions) and signed-URL TTL for screenshots, and the
public enum literal in the Fern spec, the docs spec and the vendored client."""

import json
import typing
from pathlib import Path

from skyvern.client.types.artifact_type import ArtifactType as ClientArtifactType
from skyvern.forge.sdk.artifact.manager import _REDACTABLE_TEXT_ARTIFACT_TYPES, _SCREENSHOT_PREFIX_MAP
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.artifact.signing import SENSITIVE_ARTIFACT_TYPES
from skyvern.forge.sdk.artifact.storage.base import FILE_EXTENTSION_MAP
from skyvern.forge.sdk.routes.agent_protocol import _ARTIFACT_CONTENT_TYPES

REPO = Path(__file__).resolve().parents[2]

# Written through a path that builds its own URI (uploads bucket, eval ingest, session logs, cdp proxy
# sink, legacy/deprecated values, the read-side sentinel) — not through build_uri's extension lookup.
_WRITTEN_OUTSIDE_BUILD_URI = frozenset(
    {
        ArtifactType.EVAL_SCORE,
        ArtifactType.EVAL_TRAJECTORY,
        ArtifactType.EVAL_RUBRICS,
        ArtifactType.BROWSER_SESSION_ACTION_LOG,
        ArtifactType.SCREENSHOT,
        ArtifactType.SCREENSHOT_PROXY,
        ArtifactType.HTML,
        ArtifactType.SCRIPT_FILE,
        ArtifactType.DOWNLOAD,
        ArtifactType.UNKNOWN,
    }
)
# Not step artifacts: never bundled into a STEP_ARCHIVE, so the prefix fallback cannot collide.
_NOT_STEP_SCREENSHOTS = frozenset({ArtifactType.SCREENSHOT, ArtifactType.SCREENSHOT_PROXY})


def test_every_type_has_a_storage_extension_or_is_allowlisted() -> None:
    missing = [t for t in ArtifactType if t not in FILE_EXTENTSION_MAP and t not in _WRITTEN_OUTSIDE_BUILD_URI]
    assert missing == [], f"add to FILE_EXTENTSION_MAP (first create_artifact raises KeyError otherwise): {missing}"
    stale = [t for t in _WRITTEN_OUTSIDE_BUILD_URI if t in FILE_EXTENTSION_MAP]
    assert stale == [], f"registered now; drop from the allowlist: {stale}"


def test_every_screenshot_type_is_sensitive_and_has_a_distinct_prefix() -> None:
    screenshots = [t for t in ArtifactType if t.value.startswith("screenshot")]
    assert [t for t in screenshots if t not in SENSITIVE_ARTIFACT_TYPES] == []
    missing_prefix = [t for t in screenshots if t not in _SCREENSHOT_PREFIX_MAP and t not in _NOT_STEP_SCREENSHOTS]
    assert missing_prefix == [], f"add to _SCREENSHOT_PREFIX_MAP (step-archive filenames collide): {missing_prefix}"
    assert len(set(_SCREENSHOT_PREFIX_MAP.values())) == len(_SCREENSHOT_PREFIX_MAP)
    assert [t for t in screenshots if t not in _ARTIFACT_CONTENT_TYPES and t not in _NOT_STEP_SCREENSHOTS] == []


def test_every_html_type_is_redacted_and_served_as_html() -> None:
    html_types = [t for t in ArtifactType if t.value.startswith("html")]
    assert [t for t in html_types if t not in _REDACTABLE_TEXT_ARTIFACT_TYPES] == []
    # A filled-form document is at least as sensitive as its screenshot: same short URL expiry.
    assert ArtifactType.HTML_PRE_SUBMIT in SENSITIVE_ARTIFACT_TYPES
    assert [t for t in html_types if t in FILE_EXTENTSION_MAP and t not in _ARTIFACT_CONTENT_TYPES] == []


def test_public_enum_literals_match_the_python_enum() -> None:
    values = {t.value for t in ArtifactType}
    client = set(typing.get_args(typing.get_args(ClientArtifactType)[0]))
    assert client == values, f"vendored client drift: missing={values - client} extra={client - values}"
    # docs/api-reference/openapi.json is bot-owned (regenerated from production after each deploy)
    # and deliberately not asserted here.
    spec = "fern/openapi/skyvern_openapi.json"
    enum = set(json.loads((REPO / spec).read_text())["components"]["schemas"]["ArtifactType"]["enum"])
    assert enum == values, f"{spec} drift: missing={values - enum} extra={enum - values}"
