import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.artifact import manager as artifact_manager_module
from skyvern.forge.sdk.artifact.manager import ArtifactBatchData, ArtifactManager, BulkArtifactCreationRequest
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.models import ArtifactModel
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager
from skyvern.utils.secret_redaction import (
    REDACTED_SECRET_PLACEHOLDER,
    REDACTION_FAILED_STUB,
    redact_secrets_from_json_bytes,
)
from tests.unit.forge.sdk.artifact.storage.test_helpers import (
    TEST_ORGANIZATION_ID,
    create_fake_step,
    create_fake_workflow_run_block,
)

TEST_STEP_ID = "step_secret_redaction"


def _decode_json_text_layers(value: object) -> object:
    if isinstance(value, str) and value != REDACTED_SECRET_PLACEHOLDER and value.lstrip()[:1] in ("{", "["):
        return _decode_json_text_layers(json.loads(value))
    if isinstance(value, dict):
        return {key: _decode_json_text_layers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_json_text_layers(item) for item in value]
    return value


def _strict_json(data: bytes) -> object:
    def reject(constant: str) -> object:
        raise ValueError(f"non-standard JSON constant {constant}")

    return json.loads(data.decode("utf-8"), parse_constant=reject)


class _FakeStorage:
    def __init__(self) -> None:
        self.stored: list[tuple[Artifact, bytes]] = []

    def build_uri(self, **_: object) -> str:
        return "s3://bucket/artifact"

    def build_workflow_run_block_uri(self, **_: object) -> str:
        return "s3://bucket/workflow-run-block-artifact"

    async def store_artifact(self, artifact: Artifact, data: bytes) -> None:
        self.stored.append((artifact, data))

    async def store_artifact_from_path(self, artifact: Artifact, path: str) -> None:
        self.stored.append((artifact, Path(path).read_bytes()))


class _FakeArtifactDatabase:
    async def create_artifact(
        self,
        artifact_id: str,
        artifact_type: ArtifactType,
        uri: str,
        **kwargs: object,
    ) -> Artifact:
        now = datetime.now(UTC)
        return Artifact(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            uri=uri,
            organization_id=str(kwargs["organization_id"]),
            step_id=kwargs.get("step_id"),
            task_id=kwargs.get("task_id"),
            workflow_run_id=kwargs.get("workflow_run_id"),
            workflow_run_block_id=kwargs.get("workflow_run_block_id"),
            run_id=kwargs.get("run_id"),
            created_at=now,
            modified_at=now,
        )

    async def bulk_create_artifacts(self, artifact_models: list[ArtifactModel]) -> list[Artifact]:
        return [
            await self.create_artifact(
                model.artifact_id,
                ArtifactType(model.artifact_type),
                model.uri,
                organization_id=model.organization_id,
                task_id=model.task_id,
                step_id=model.step_id,
                workflow_run_id=model.workflow_run_id,
            )
            for model in artifact_models
        ]


@pytest.fixture
def artifact_redaction_setup(
    monkeypatch: pytest.MonkeyPatch,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
) -> _FakeStorage:
    storage = _FakeStorage()
    monkeypatch.setattr(artifact_manager_module.settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)
    monkeypatch.setattr(artifact_manager_module.app, "STORAGE", storage)
    monkeypatch.setattr(
        artifact_manager_module.app,
        "DATABASE",
        SimpleNamespace(artifacts=_FakeArtifactDatabase()),
    )
    monkeypatch.setattr(
        artifact_manager_module.app,
        "WORKFLOW_CONTEXT_MANAGER",
        workflow_context_manager_factory(
            workflow_run_id="wr_redact",
            mask_secrets=True,
            secrets={"password": "secret-value"},
        ),
    )
    skyvern_context.reset()
    skyvern_context.set(SkyvernContext(organization_id=TEST_ORGANIZATION_ID, workflow_run_id="wr_redact"))
    yield storage
    skyvern_context.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("artifact_type", "data"),
    [
        (ArtifactType.HTML_SCRAPE, b"<html>secret-value</html>"),
        (ArtifactType.HASHED_HREF_MAP, b'{"h1": "https://example.test/callback?token=secret-value"}'),
        # Too deep for the value walk once a duplicate key sends it to the token pass, so it is scrubbed as text.
        pytest.param(
            ArtifactType.LLM_REQUEST,
            ('{"k": 1, "k": 2, "c": ' + json.dumps("[" * 2000 + '"secret-value"' + "]" * 2000) + "}").encode(),
            id="deep_text_beside_duplicate_key",
        ),
        # On Python 3.11 only a depth near 800 reaches the token-pass guard; the 2,000-level row covers 3.12 and later.
        pytest.param(
            ArtifactType.LLM_REQUEST,
            ('{"k": 1, "k": 2, "c": ' + json.dumps("[" * 800 + '"secret-value"' + "]" * 800) + "}").encode(),
            id="text_800_levels_beside_duplicate_key",
        ),
    ],
)
async def test_create_artifact_redacts_textual_artifact_data(
    artifact_redaction_setup: _FakeStorage, artifact_type: ArtifactType, data: bytes
) -> None:
    manager = ArtifactManager()
    step = create_fake_step(TEST_STEP_ID)

    await manager.create_artifact(step=step, artifact_type=artifact_type, data=data)
    await manager.wait_for_upload_aiotasks([step.task_id])

    assert artifact_redaction_setup.stored[0][1] == data.replace(b"secret-value", REDACTED_SECRET_PLACEHOLDER.encode())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("artifact_type", "data", "failure"),
    [
        (ArtifactType.HTML_SCRAPE, b"<html>secret-value</html>", "lookup"),
        (ArtifactType.LLM_REQUEST, b'{"messages": [{"role": "user", "content": "secret-value"}]}', "lookup"),
        (
            ArtifactType.HAR,
            b'{"log":{"entries":[{"request":{"headers":[{"name":"Authorization","value":"Bearer token"}]}}]}}',
            "lookup",
        ),
        (
            ArtifactType.HAR,
            b'{"log":{"entries":[{"request":{"headers":[{"name":"Authorization","value":"Bearer token"}]}}]}}',
            "lookup_and_har_scrub",
        ),
        (ArtifactType.HTML_SCRAPE, b"<html>secret-value</html>", "lookup_with_redaction_switched_off"),
    ],
)
async def test_create_artifact_never_stores_raw_data_when_secret_lookup_fails(
    artifact_redaction_setup: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    artifact_type: ArtifactType,
    data: bytes,
    failure: str,
) -> None:
    def failing_lookup(*_: object, **__: object) -> set[str]:
        raise RuntimeError("secret store unavailable")

    def failing_har_scrub(*_: object, **__: object) -> bytes:
        raise RuntimeError("HAR scrub failed")

    monkeypatch.setattr(
        artifact_manager_module.app.WORKFLOW_CONTEXT_MANAGER, "get_secret_values_for_run", failing_lookup
    )
    if failure == "lookup_and_har_scrub":
        monkeypatch.setattr(artifact_manager_module, "redact_har_bytes", failing_har_scrub)
    if failure == "lookup_with_redaction_switched_off":
        # With the switch off the run's opt-in is never read, so the failure has to come from that read.
        monkeypatch.setattr(artifact_manager_module.settings, "ENABLE_SECRET_ARTIFACT_REDACTION", False)
        monkeypatch.setattr(
            artifact_manager_module.app.WORKFLOW_CONTEXT_MANAGER, "artifact_redaction_enabled", failing_lookup
        )
    manager = ArtifactManager()
    step = create_fake_step(TEST_STEP_ID)

    await manager.create_artifact(step=step, artifact_type=artifact_type, data=data)
    await manager.wait_for_upload_aiotasks([step.task_id])

    stored = artifact_redaction_setup.stored[0][1]
    if failure == "lookup_with_redaction_switched_off":
        # The kill switch restores the pre-redaction behavior: the artifact is stored as produced.
        assert stored == data
    elif artifact_type == ArtifactType.HAR and failure == "lookup":
        # The HAR keeps its structure; the name-based scrub still removes the credential header value.
        assert b"Bearer token" not in stored
        assert json.loads(stored)["log"]["entries"][0]["request"]["headers"][0]["value"] == REDACTED_SECRET_PLACEHOLDER
    else:
        assert stored == REDACTION_FAILED_STUB


@pytest.mark.asyncio
async def test_create_artifact_redacts_registered_bare_task_secret(
    artifact_redaction_setup: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(artifact_manager_module.app, "WORKFLOW_CONTEXT_MANAGER", WorkflowContextManager())
    manager = ArtifactManager()
    step = create_fake_step(TEST_STEP_ID)
    context = SkyvernContext(
        organization_id=TEST_ORGANIZATION_ID,
        task_id=step.task_id,
        workflow_run_id=None,
    )
    context.register_secret_value("482913")
    skyvern_context.set(context)

    await manager.create_artifact(
        step=step,
        artifact_type=ArtifactType.LLM_REQUEST,
        data=b'{"role": "tool", "content": "verification_code: 482913"}',
    )
    await manager.wait_for_upload_aiotasks([step.task_id])

    artifact, stored = artifact_redaction_setup.stored[0]
    assert artifact.workflow_run_id is None
    assert b"482913" not in stored
    assert REDACTED_SECRET_PLACEHOLDER.encode() in stored


@pytest.mark.asyncio
async def test_create_artifact_redacts_using_artifact_workflow_run_id_without_context(
    monkeypatch: pytest.MonkeyPatch,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
) -> None:
    storage = _FakeStorage()
    monkeypatch.setattr(artifact_manager_module.settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)
    monkeypatch.setattr(artifact_manager_module.app, "STORAGE", storage)
    monkeypatch.setattr(
        artifact_manager_module.app,
        "DATABASE",
        SimpleNamespace(artifacts=_FakeArtifactDatabase()),
    )
    monkeypatch.setattr(
        artifact_manager_module.app,
        "WORKFLOW_CONTEXT_MANAGER",
        workflow_context_manager_factory(
            workflow_run_id="wr_redact",
            mask_secrets=True,
            secrets={"password": "secret-value"},
        ),
    )
    skyvern_context.reset()
    assert skyvern_context.current() is None

    manager = ArtifactManager()
    workflow_run_block = create_fake_workflow_run_block("wr_redact", "wrb_redact")

    await manager.create_workflow_run_block_artifact(
        workflow_run_block=workflow_run_block,
        artifact_type=ArtifactType.HTML_SCRAPE,
        data=b"<html>secret-value</html>",
    )
    await manager.wait_for_upload_aiotasks([workflow_run_block.workflow_run_block_id])

    assert storage.stored[0][0].workflow_run_id == "wr_redact"
    assert storage.stored[0][1] == f"<html>{REDACTED_SECRET_PLACEHOLDER}</html>".encode()


@pytest.mark.asyncio
async def test_create_artifact_leaves_binary_artifact_data_unchanged(
    artifact_redaction_setup: _FakeStorage,
) -> None:
    manager = ArtifactManager()
    step = create_fake_step(TEST_STEP_ID)

    await manager.create_artifact(
        step=step,
        artifact_type=ArtifactType.SCREENSHOT_LLM,
        data=b"png secret-value bytes",
    )
    await manager.wait_for_upload_aiotasks([step.task_id])

    assert artifact_redaction_setup.stored[0][1] == b"png secret-value bytes"


@pytest.mark.asyncio
async def test_create_artifact_leaves_textual_artifact_data_unchanged_when_flag_disabled(
    artifact_redaction_setup: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(artifact_manager_module.settings, "ENABLE_SECRET_ARTIFACT_REDACTION", False)
    manager = ArtifactManager()
    step = create_fake_step(TEST_STEP_ID)

    await manager.create_artifact(
        step=step,
        artifact_type=ArtifactType.HTML_SCRAPE,
        data=b"<html>secret-value</html>",
    )
    await manager.wait_for_upload_aiotasks([step.task_id])

    assert artifact_redaction_setup.stored[0][1] == b"<html>secret-value</html>"


def _har_with_session_material(base64_body: bool) -> bytes:
    body = '{"echo": "secret-value", "code": "482913", "n": 94829130, "msg": "code:\\n482913"}'
    content = (
        {"mimeType": "application/json", "encoding": "base64", "text": base64.b64encode(body.encode()).decode()}
        if base64_body
        else {"mimeType": "application/json", "text": body}
    )
    return json.dumps(
        {
            "log": {
                "entries": [
                    {
                        "request": {
                            # The task's runtime code touches other digits in the URL, a JSON number and a body.
                            "url": "https://example.test/login?ref=7482913",
                            "headers": [
                                {"name": "Cookie", "value": "sid=cookie-value-1"},
                                {"name": "Authorization", "value": "Bearer bearer-value-1"},
                            ],
                            "cookies": [{"name": "sid", "value": "cookie-value-1"}],
                            "postData": {
                                "mimeType": "application/x-www-form-urlencoded",
                                "text": "",
                                "params": [
                                    {"name": "username", "value": "someone"},
                                    {"name": "password", "value": "form-password-1"},
                                ],
                            },
                        },
                        "response": {
                            "headers": [
                                {"name": "Set-Cookie", "value": "sid=cookie-value-2; Path=/"},
                                {"name": "Content-Type", "value": "application/json"},
                            ],
                            "cookies": [{"name": "sid", "value": "cookie-value-2"}],
                            "bodySize": 4829135,
                            "content": content,
                        },
                    }
                ]
            }
        }
    ).encode()


async def _check_opted_out_run_secrets_only_under_all_runs_switch(
    artifact_redaction_setup: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
    scrub_all_runs: bool | None,
) -> None:
    # None runs on the shipped default, which is on.
    if scrub_all_runs is not None:
        monkeypatch.setattr(artifact_manager_module.settings, "ENABLE_ARTIFACT_VALUE_SCRUB_ALL_RUNS", scrub_all_runs)
    context_manager = workflow_context_manager_factory(
        workflow_run_id="wr_redact",
        mask_secrets=False,
        secrets={
            "password": "secret-value",
            "username": "admin",
            "code": "12345678",
            "quoted": 'quo"ted-pass',
            "doc": '{"demo": "fixture-only"}',
            "spanning": 'alpha", "bravo',
        },
    )
    monkeypatch.setattr(artifact_manager_module.app, "WORKFLOW_CONTEXT_MANAGER", context_manager)
    manager = ArtifactManager()
    step = create_fake_step(TEST_STEP_ID)
    html = b"<html>secret-value admin</html>"
    har = json.dumps({"log": {"entries": [{"response": {"content": {"text": "secret-value"}}}]}}).encode()
    # A model reply whose message content is itself JSON text, holding a number-valued and a quoted secret.
    reply = json.dumps({"reference": 12345678, "password": 'quo"ted-pass'})
    llm = json.dumps({"choices": [{"message": {"content": reply}}]}).encode()
    # A duplicate key would drop the earlier copy in a parse; a long float would round its digits away.
    request = b'{"n": 123456789012345678901.5}'
    duplicated = b'{"password": "secret-value", "password": "x"}'
    # The quoted secret is escaped twice in the raw bytes, so only a decoded read finds it.
    rendered = json.dumps({"content": json.dumps({"password": 'quo"ted-pass'})}).encode()
    # A registered secret that is itself a JSON document, shown as an element's text.
    tree = json.dumps([{"id": "a", "text": '{"demo": "fixture-only"}'}]).encode()
    # Each JSON case lists the stored value with every JSON text layer decoded, and must stay parseable.
    json_cases = [
        (
            ArtifactType.LLM_RESPONSE,
            json.dumps({"choices": [{"message": {"content": json.dumps({"reference": 12345678})}}]}),
            {"choices": [{"message": {"content": {"reference": REDACTED_SECRET_PLACEHOLDER}}}]},
        ),
        (ArtifactType.LLM_RESPONSE_PARSED, "12345678", REDACTED_SECRET_PLACEHOLDER),
        (ArtifactType.LLM_RESPONSE_PARSED, json.dumps("secret-value"), REDACTED_SECRET_PLACEHOLDER),
        (ArtifactType.LLM_RESPONSE_PARSED, '{"demo": "fixture-only"}', REDACTED_SECRET_PLACEHOLDER),
        (
            ArtifactType.LLM_RESPONSE_PARSED,
            '{"payload": {"demo": "fixture-only"}}',
            {"payload": REDACTED_SECRET_PLACEHOLDER},
        ),
        (ArtifactType.LLM_RESPONSE_PARSED, '["alpha", "bravo"]', [REDACTED_SECRET_PLACEHOLDER]),
        (
            ArtifactType.LLM_RESPONSE_RENDERED,
            json.dumps({"c": json.dumps({"d": {"demo": "fixture-only"}, "p": "secret-value"})}),
            {"c": {"d": REDACTED_SECRET_PLACEHOLDER, "p": REDACTED_SECRET_PLACEHOLDER}},
        ),
        # A document-valued secret written in another formatting, as JSON text with no escapes in it.
        (
            ArtifactType.LLM_RESPONSE_RENDERED,
            json.dumps({"c": json.dumps({"k": {"demo": "fixture-only"}}, separators=(",", ":"))}),
            {"c": {"k": REDACTED_SECRET_PLACEHOLDER}},
        ),
        # Half of a split emoji cannot be written as UTF-8 unless it is escaped.
        (
            ArtifactType.VISIBLE_ELEMENTS_TREE,
            json.dumps({"t": "\ud83d", "n": 12345678, "secret-value": 1}),
            {"t": "\ud83d", "n": REDACTED_SECRET_PLACEHOLDER, REDACTED_SECRET_PLACEHOLDER: 1},
        ),
        # Past the double range, so it parses as inf and cannot be written back as standard JSON.
        (
            ArtifactType.VISIBLE_ELEMENTS_TREE,
            '{"big": 1e400, "p": "secret-value"}',
            {"big": float("inf"), "p": REDACTED_SECRET_PLACEHOLDER},
        ),
    ]
    # Each runs on its own secrets, so the spelling screen decides whether the artifact is parsed.
    field = json.dumps({"password": 'quo"ted-pass'})
    quoted = {"password": "secret-value", "quoted": 'quo"ted-pass'}
    numeric_document = {"a": 1234, "b": 5678}
    spanning = {"password": "secret-value", "spanning": 'alpha", "bravo'}
    screened_cases = [
        # A form field's JSON text inside tool arguments inside the request: three escaping layers.
        (
            quoted,
            ArtifactType.LLM_REQUEST,
            json.dumps({"messages": [{"content": json.dumps({"text": field})}]}),
            {"messages": [{"content": {"text": {"password": REDACTED_SECRET_PLACEHOLDER}}}]},
        ),
        # Four layers, so the secret is escaped too often for a text match before the walk decodes it.
        (
            quoted,
            ArtifactType.LLM_REQUEST,
            json.dumps({"a": json.dumps({"b": json.dumps({"c": json.dumps({"d": field})})})}),
            {"a": {"b": {"c": {"d": {"password": REDACTED_SECRET_PLACEHOLDER}}}}},
        ),
        (
            quoted,
            ArtifactType.VISIBLE_ELEMENTS_TREE,
            json.dumps({"c": '{"p": "s\\u0065cret-value"}'}),
            {"c": {"p": REDACTED_SECRET_PLACEHOLDER}},
        ),
        (quoted, ArtifactType.VISIBLE_ELEMENTS_TREE, '{"p": "secret-va\\u006Cue"}', {"p": REDACTED_SECRET_PLACEHOLDER}),
        (quoted, ArtifactType.VISIBLE_ELEMENTS_TREE, '{"p": "\\u0073ecret-value"}', {"p": REDACTED_SECRET_PLACEHOLDER}),
        # Text that cannot round-trip through a parse still has its nested JSON layers scrubbed.
        (
            quoted,
            ArtifactType.VISIBLE_ELEMENTS_TREE,
            '{"k": 1, "k": 2, "c": ' + json.dumps(field) + "}",
            {"k": 2, "c": {"password": REDACTED_SECRET_PLACEHOLDER}},
        ),
        (
            quoted,
            ArtifactType.VISIBLE_ELEMENTS_TREE,
            '{"big": 1e400, "c": ' + json.dumps(field) + "}",
            {"big": float("inf"), "c": {"password": REDACTED_SECRET_PLACEHOLDER}},
        ),
        (
            quoted,
            ArtifactType.VISIBLE_ELEMENTS_TREE,
            json.dumps({"c": '{"k": 1, "k": 2, "q": ' + json.dumps(field) + "}"}),
            {"c": {"k": 2, "q": {"password": REDACTED_SECRET_PLACEHOLDER}}},
        ),
        (
            {"code": "12345678"},
            ArtifactType.VISIBLE_ELEMENTS_TREE,
            '{"n": 1.2345678e7, "s": "12345678"}',
            {"n": REDACTED_SECRET_PLACEHOLDER, "s": REDACTED_SECRET_PLACEHOLDER},
        ),
        (
            {"code": "12345678"},
            ArtifactType.VISIBLE_ELEMENTS_TREE,
            '{"k":1,"k":2,"n":12345678}',
            {"k": 2, "n": REDACTED_SECRET_PLACEHOLDER},
        ),
        (
            spanning,
            ArtifactType.VISIBLE_ELEMENTS_TREE,
            '{"k": 1, "k": 2, "l": ["alpha", "bravo"]}',
            {"k": 2, "l": [REDACTED_SECRET_PLACEHOLDER]},
        ),
        (
            {**spanning, **quoted},
            ArtifactType.VISIBLE_ELEMENTS_TREE,
            json.dumps({"l": ["alpha", "bravo"], "c": field}),
            {"l": [REDACTED_SECRET_PLACEHOLDER], "c": {"password": REDACTED_SECRET_PLACEHOLDER}},
        ),
        (
            {"path": "a/b/secret-path"},
            ArtifactType.VISIBLE_ELEMENTS_TREE,
            '{"p": "a\\/b\\/secret-path"}',
            {"p": REDACTED_SECRET_PLACEHOLDER},
        ),
        # No character of this secret is printable ASCII, so no literal run can screen it out.
        (
            {"name": "ÄÖÜßäöüé"},
            ArtifactType.VISIBLE_ELEMENTS_TREE,
            json.dumps({"p": "ÄÖÜßäöüé"}, ensure_ascii=False),
            {"p": REDACTED_SECRET_PLACEHOLDER},
        ),
        # A match spanning keys and values inside nested JSON text is found by rescanning that text.
        (
            spanning,
            ArtifactType.VISIBLE_ELEMENTS_TREE,
            json.dumps({"c": json.dumps({"d": json.dumps({"p": "secret-value", "l": ["alpha", "bravo"]})})}),
            {"c": {"d": {"p": REDACTED_SECRET_PLACEHOLDER, "l": [REDACTED_SECRET_PLACEHOLDER]}}},
        ),
        # A document secret is found by its keys and string leaves in any formatting; one with neither
        # is always parsed.
        *[
            (
                {"doc": json.dumps(document)},
                ArtifactType.LLM_RESPONSE_PARSED,
                json.dumps({"x": document}, indent=indent, separators=separators),
                {"x": REDACTED_SECRET_PLACEHOLDER},
            )
            for document in (numeric_document, [1234, 5678])
            for indent, separators in ((None, (",", ":")), (2, None))
        ],
        # A registered number inside a document secret does not keep the whole document from matching.
        (
            {"code": "12345678", "doc": '{"user": "svc-user-1", "pin": 12345678}'},
            ArtifactType.LLM_RESPONSE_PARSED,
            json.dumps({"cfg": {"user": "svc-user-1", "pin": 12345678}}),
            {"cfg": REDACTED_SECRET_PLACEHOLDER},
        ),
    ]
    # The registered number touches other digits in a URL, HAR numbers and bodies, with and without a base64
    # body; in the third HAR only the numbers hold it, and in the last its digits only follow a JSON escape.
    har_digit_cases = [
        json.dumps(
            {
                "log": {
                    "entries": [
                        {
                            "request": {"url": url},
                            "response": {"bodySize": 1123456789, "content": {"size": 12345678, **content}},
                        }
                    ]
                }
            }
        ).encode()
        for url, content in (
            ("https://example.test/a?q=912345678", {"text": "ref=0123456789"}),
            (
                "https://example.test/a?q=912345678",
                {"encoding": "base64", "text": base64.b64encode(b"ref=0123456789").decode()},
            ),
            ("https://example.test/a", {"text": "plain"}),
            ("https://example.test/a", {"text": "\u54812345678"}),
        )
    ]
    # A document secret keeps the screen: JSON holding none of the run's secrets is returned without a parse.
    unparsed = b'{"k": "x", "k": "y"}'
    assert redact_secrets_from_json_bytes(unparsed, {"secret-value", '{"demo": "fixture-only"}'}) is unparsed

    await manager.create_artifact(step=step, artifact_type=ArtifactType.HTML_SCRAPE, data=html)
    await manager.create_artifact(step=step, artifact_type=ArtifactType.HAR, data=har)
    await manager.create_artifact(step=step, artifact_type=ArtifactType.LLM_RESPONSE, data=llm)
    await manager.create_artifact(step=step, artifact_type=ArtifactType.LLM_REQUEST, data=request)
    await manager.create_artifact(step=step, artifact_type=ArtifactType.LLM_RESPONSE_PARSED, data=duplicated)
    await manager.create_artifact(step=step, artifact_type=ArtifactType.LLM_RESPONSE_RENDERED, data=rendered)
    await manager.create_artifact(step=step, artifact_type=ArtifactType.VISIBLE_ELEMENTS_TREE, data=tree)
    for artifact_type, data, _ in json_cases:
        await manager.create_artifact(step=step, artifact_type=artifact_type, data=data.encode())
    await manager.wait_for_upload_aiotasks([step.task_id])
    for har_case in har_digit_cases:
        await manager.create_artifact(step=step, artifact_type=ArtifactType.HAR, data=har_case)
    await manager.wait_for_upload_aiotasks([step.task_id])
    for secrets, artifact_type, data, _ in screened_cases:
        screened_context = workflow_context_manager_factory(
            workflow_run_id="wr_redact", mask_secrets=False, secrets=secrets
        )
        monkeypatch.setattr(artifact_manager_module.app, "WORKFLOW_CONTEXT_MANAGER", screened_context)
        await manager.create_artifact(step=step, artifact_type=artifact_type, data=data.encode())
        await manager.wait_for_upload_aiotasks([step.task_id])

    har_digit_end = 7 + len(json_cases) + len(har_digit_cases)
    har_digit_stored = [data for _, data in artifact_redaction_setup.stored[7 + len(json_cases) : har_digit_end]]
    json_stored = [
        data
        for _, data in artifact_redaction_setup.stored[7 : 7 + len(json_cases)]
        + artifact_redaction_setup.stored[har_digit_end:]
    ]
    stored = {artifact.artifact_type: data for artifact, data in artifact_redaction_setup.stored[:7]}
    if scrub_all_runs is not False:
        # A short value matches on word boundaries and would rewrite ordinary text, so it is kept.
        assert stored[ArtifactType.HTML_SCRAPE] == f"<html>{REDACTED_SECRET_PLACEHOLDER} admin</html>".encode()
        assert b"secret-value" not in stored[ArtifactType.HAR]
        content = json.loads(stored[ArtifactType.LLM_RESPONSE])["choices"][0]["message"]["content"]
        assert json.loads(content) == {
            "reference": REDACTED_SECRET_PLACEHOLDER,
            "password": REDACTED_SECRET_PLACEHOLDER,
        }
        assert json.loads(stored[ArtifactType.LLM_REQUEST]) == {"n": REDACTED_SECRET_PLACEHOLDER}
        assert b"secret-value" not in stored[ArtifactType.LLM_RESPONSE_PARSED]
        assert stored[ArtifactType.LLM_RESPONSE_PARSED].count(b'"password"') == 2
        rendered_content = json.loads(json.loads(stored[ArtifactType.LLM_RESPONSE_RENDERED])["content"])
        assert rendered_content == {"password": REDACTED_SECRET_PLACEHOLDER}
        assert json.loads(stored[ArtifactType.VISIBLE_ELEMENTS_TREE]) == [
            {"id": "a", "text": REDACTED_SECRET_PLACEHOLDER}
        ]
        assert [_decode_json_text_layers(_strict_json(data)) for data in json_stored] == [
            *[expected for _, _, expected in json_cases],
            *[expected for *_, expected in screened_cases],
        ]
        for har_stored in har_digit_stored:
            har_entry = _strict_json(har_stored)
            assert isinstance(har_entry, dict)
            response = har_entry["log"]["entries"][0]["response"]
            har_content = response["content"]
            har_body = (
                base64.b64decode(har_content["text"]).decode() if "encoding" in har_content else har_content["text"]
            )
            assert response["bodySize"] == har_content["size"] == REDACTED_SECRET_PLACEHOLDER
            assert "12345678" not in json.dumps(har_entry, ensure_ascii=False)
            assert har_body in (f"ref=0{REDACTED_SECRET_PLACEHOLDER}9", "plain", "\u54812345678")
    else:
        assert har_digit_stored == har_digit_cases
        assert stored == {
            ArtifactType.HTML_SCRAPE: html,
            ArtifactType.HAR: har,
            ArtifactType.LLM_RESPONSE: llm,
            ArtifactType.LLM_REQUEST: request,
            ArtifactType.LLM_RESPONSE_PARSED: duplicated,
            ArtifactType.LLM_RESPONSE_RENDERED: rendered,
            ArtifactType.VISIBLE_ELEMENTS_TREE: tree,
        }
        assert json_stored == [
            *[data.encode() for _, data, _ in json_cases],
            *[data.encode() for _, _, data, _ in screened_cases],
        ]
    # The prompt and row scrubs read the per-workflow opt-in, which stays off.
    assert context_manager.get_secret_values_for_run("wr_redact") == set()


async def _check_har_structured_fields_whether_or_not_workflow_opted_in(
    artifact_redaction_setup: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
    mask_secrets: bool,
    lookup_fails: bool,
    source: str,
    base64_body: bool,
) -> None:
    context_manager = workflow_context_manager_factory(
        workflow_run_id="wr_redact", mask_secrets=mask_secrets, secrets={"password": "secret-value"}
    )
    if lookup_fails:

        def failing_lookup(*_: object, **__: object) -> set[str]:
            raise RuntimeError("secret store unavailable")

        monkeypatch.setattr(context_manager, "get_secret_values_for_run", failing_lookup)
    monkeypatch.setattr(artifact_manager_module.app, "WORKFLOW_CONTEXT_MANAGER", context_manager)
    runtime_context = skyvern_context.current()
    assert runtime_context is not None
    runtime_context.runtime_secret_values.add("482913")
    manager = ArtifactManager()
    step = create_fake_step(TEST_STEP_ID)
    har_path = tmp_path / "run.har"
    har_path.write_bytes(_har_with_session_material(base64_body))

    if source == "data":
        await manager.create_artifact(step=step, artifact_type=ArtifactType.HAR, data=har_path.read_bytes())
    elif source == "path":
        await manager.create_artifact(step=step, artifact_type=ArtifactType.HAR, path=str(har_path))
    else:
        artifact_model = ArtifactModel(
            artifact_id="a_har_bulk",
            artifact_type=ArtifactType.HAR,
            uri="s3://bucket/artifact",
            organization_id=TEST_ORGANIZATION_ID,
            task_id=step.task_id,
            step_id=step.step_id,
            workflow_run_id="wr_redact",
        )
        await manager.bulk_create_artifacts(
            [
                BulkArtifactCreationRequest(
                    artifacts=[ArtifactBatchData(artifact_model=artifact_model, path=str(har_path))],
                    primary_key=step.task_id,
                )
            ]
        )
    await manager.wait_for_upload_aiotasks([step.task_id])

    stored = artifact_redaction_setup.stored[0][1]
    entry = json.loads(stored)["log"]["entries"][0]
    if base64_body:
        stored += base64.b64decode(entry["response"]["content"]["text"])
    for leaked in (b"cookie-value-1", b"cookie-value-2", b"bearer-value-1", b"form-password-1"):
        assert leaked not in stored
    # Antibot detection reads cookie names and the submission verifier reads content-type.
    assert [cookie["name"] for cookie in entry["request"]["cookies"]] == ["sid"]
    assert [cookie["name"] for cookie in entry["response"]["cookies"]] == ["sid"]
    assert {"name": "Content-Type", "value": "application/json"} in entry["response"]["headers"]
    assert {"name": "username", "value": "someone"} in entry["request"]["postData"]["params"]
    # A run whose secret lookup succeeded also scrubs its credential values from bodies; an opted-out run
    # does so under the all-runs switch, which is on by default.
    assert (b"secret-value" in stored) is lookup_fails
    # The one-time code comes from the task, so only the opted-in lookup's failure loses it.
    assert (b"482913" in stored) is (mask_secrets and lookup_fails)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        *[("all_runs_switch", scrub_all_runs) for scrub_all_runs in (None, True, False)],
        *[
            ("har_fields", mask_secrets, lookup_fails, source, base64_body)
            for mask_secrets, lookup_fails in ((True, False), (False, False), (True, True), (False, True))
            for source in ("data", "path", "bulk_path")
            for base64_body in (False, True)
        ],
    ],
    ids=str,
)
async def test_create_artifact_scrubs_har_fields_and_opted_out_run_secrets(
    artifact_redaction_setup: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
    case: tuple[str | bool | None, ...],
) -> None:
    if case[0] == "all_runs_switch":
        await _check_opted_out_run_secrets_only_under_all_runs_switch(
            artifact_redaction_setup, monkeypatch, workflow_context_manager_factory, *case[1:]
        )
    else:
        await _check_har_structured_fields_whether_or_not_workflow_opted_in(
            artifact_redaction_setup, monkeypatch, tmp_path, workflow_context_manager_factory, *case[1:]
        )
