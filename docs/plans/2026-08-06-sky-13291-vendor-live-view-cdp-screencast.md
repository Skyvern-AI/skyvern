# Vendor Live View via Per-Session CDP Screencast (SKY-13291 slice 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Amended after review — the run surfaces are out of scope, and the viewer connects differently.**
> Tasks 1–3 below plan a per-session gate on the task and workflow-run stream endpoints, backed by
> a fallback that resolves a run's browser through its session. Review established that both parts
> were wrong, and what shipped differs:
>
> - **Run surfaces keep the deployment-global gate.** A run's worker already publishes frames for a
>   remote-CDP browser (`CDPFramePublisher` → `StorageBase.get_streaming_file`), which is how those
>   endpoints already serve an externally hosted session. Routing them per session bought nothing —
>   and missed anyway, because a task inside a workflow leases its session under the *run's* id, so
>   resolving by task id never matched. `verify.browser_session_id_for_runnable` was dropped.
> - **The viewer no longer reuses `get_browser_state`.** For an externally hosted session that adopts
>   the browser through the Fetch-download factory, which would arm a second download interceptor on
>   pages a run is driving and leave the connection in a cache a run later reads. Watching now goes
>   through `get_observer_browser_state` / `release_observer_browser_state`: one uncached,
>   interception-free connection per websocket, given back when the viewer leaves.
>
> The per-session gate on the browser-session endpoint, the `stream_transport` field, the frontend
> selection, and the capability flip are as planned.

**Goal:** Make live-stream transport a per-session decision instead of the deployment-global `BROWSER_STREAMING_MODE`, so external-provider ("vendor") browser sessions stream via CDP `Page.startScreencast` through the session router while first-party sessions keep VNC.

**Architecture:** A new `AgentFunction.resolve_stream_transport` hook answers `"vnc"` or `"cdp"` per session (OSS base returns the global setting; the cloud override answers `"cdp"` for vendor-held rows from `browser_session_infra`). The four backend serving-path gates that read `settings.BROWSER_STREAMING_MODE` switch to the hook, the CDP screencast path learns to resolve a task/workflow-run's browser state through `PERSISTENT_SESSIONS_MANAGER` (remote `connect_over_cdp` on the routed address) when the in-process `BROWSER_MANAGER` has none, `BrowserSessionResponse` grows a `stream_transport` field, and the frontend's 7 stream-selection call sites consult the session's transport with the global mode as fallback. The final, separately-revertable step flips `live_view=True` in the vendor capability table, which simultaneously permits routing `needs_live_view` sessions to vendors and (via the existing `supports_live_view` override, which already delegates to the capability table) makes `vnc_streaming_supported` true for vendor sessions.

**Tech Stack:** Python/FastAPI (skyvern OSS + cloud overlay), Playwright CDP sessions, React/TypeScript + react-query (skyvern-frontend), pytest (`@pytest.mark.asyncio`), vitest.

**Context (verified 2026-08-06):** Both vendors are spike-proven for `Page.startScreencast` on the shared driver connection (41–53 fps, zero driver interference, second concurrent CDP connection accepted). See the SKY-13291 Linear comments for numbers. The viewer stack (screencast loop, CDP input channel, `InteractiveStreamView`) already exists from OSS local mode.

## Global Constraints

- **OSS boundary:** never import `cloud.*` from `skyvern/`. All cloud-specific logic goes through the `AgentFunction` override in `cloud/agent_functions.py`.
- **OSS sync / non-disclosure:** `skyvern/`, `tests/unit*/` and `docs/` are all synced to the public repo (see `.github/sync.yml` for the full list — this document is inside that surface). No provider name may appear anywhere in it — say "external provider". Provider-specific logic and tests live in `cloud/` and `tests/cloud/`.
- Line length 120; `structlog` (`LOG = structlog.get_logger()`), imports at top of file.
- Comments: only for non-obvious constraints; no change-log or task-reference comments.
- Tests: extend existing `test_X` files for module X; check `tests/unit/conftest.py` fixtures before writing fakes; no exact-prompt or mock-call-count assertions.
- Branch naming: `{username}/sky-13291-{short-description}`. Commits use conventional messages. No migrations are needed (no schema changes — `stream_transport` is computed, not stored).
- PR bodies use `.github/pull_request_template.md` (Problem / Solution / How Has This Been Tested?); validate with `python3 .github/scripts/validate_pr_body.py .github/pull_request_template.md <body-file>`.
- Validation floor per modified Python file: `uv run python -m py_compile <file>`; frontend: `cd skyvern-frontend && npx tsc --noEmit` and `npx eslint <files>`.
- Suggested PR split: Tasks 1–5 (backend), Tasks 6–7 (frontend), Task 8 (capability flip — its own PR, the rollout switch).

---

### Task 1: `resolve_stream_transport` AgentFunction + resolution helpers

**Files:**
- Modify: `skyvern/forge/agent_functions.py` (next to `supports_live_view`, ~line 917)
- Modify: `skyvern/forge/sdk/routes/streaming/verify.py` (module level, after `verify_browser_session`)
- Test: `tests/unit/forge/sdk/routes/streaming/test_stream_transport.py` (new)

**Interfaces:**
- Consumes: `settings.BROWSER_STREAMING_MODE` (str, `"vnc"` default), `app.AGENT_FUNCTION`, `app.PERSISTENT_SESSIONS_MANAGER.get_session_by_runnable_id(organization_id=..., runnable_id=...)`.
- Produces (later tasks rely on these exact names):
  - `AgentFunction.resolve_stream_transport(*, browser_session_id: str | None, organization_id: str | None) -> str`
  - `verify.stream_transport(browser_session_id: str | None, organization_id: str) -> str` (never raises)
  - `verify.browser_session_id_for_runnable(runnable_id: str, organization_id: str) -> str | None` (never raises)

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/forge/sdk/routes/streaming/test_stream_transport.py`:

```python
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.routes.streaming import verify


@pytest.mark.asyncio
async def test_stream_transport_delegates_to_agent_function(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "AGENT_FUNCTION", AsyncMock(resolve_stream_transport=AsyncMock(return_value="cdp")))

    assert await verify.stream_transport("pbs_1", "org_1") == "cdp"
    app.AGENT_FUNCTION.resolve_stream_transport.assert_awaited_once_with(
        browser_session_id="pbs_1", organization_id="org_1"
    )


@pytest.mark.asyncio
async def test_stream_transport_falls_back_to_setting_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app, "AGENT_FUNCTION", AsyncMock(resolve_stream_transport=AsyncMock(side_effect=RuntimeError("db down")))
    )
    monkeypatch.setattr(verify.settings, "BROWSER_STREAMING_MODE", "vnc")

    assert await verify.stream_transport("pbs_1", "org_1") == "vnc"


@pytest.mark.asyncio
async def test_base_agent_function_returns_deployment_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.agent_functions import AgentFunction

    monkeypatch.setattr(verify.settings, "BROWSER_STREAMING_MODE", "cdp")

    assert await AgentFunction().resolve_stream_transport(browser_session_id=None, organization_id=None) == "cdp"


@pytest.mark.asyncio
async def test_browser_session_id_for_runnable_resolves_and_swallows(monkeypatch: pytest.MonkeyPatch) -> None:
    session = AsyncMock()
    session.persistent_browser_session_id = "pbs_9"
    manager = AsyncMock(get_session_by_runnable_id=AsyncMock(return_value=session))
    monkeypatch.setattr(app, "PERSISTENT_SESSIONS_MANAGER", manager)

    assert await verify.browser_session_id_for_runnable("wr_1", "org_1") == "pbs_9"

    manager.get_session_by_runnable_id.side_effect = RuntimeError("db down")
    assert await verify.browser_session_id_for_runnable("wr_1", "org_1") is None
```

If this directory's existing tests use a different async marker or app-patching fixture (see `test_verify_browser_session.py`), match that style instead.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/forge/sdk/routes/streaming/test_stream_transport.py -v`
Expected: FAIL — `AttributeError` / `ImportError` (no `resolve_stream_transport`, no `verify.stream_transport`).

- [ ] **Step 3: Implement the AgentFunction base**

In `skyvern/forge/agent_functions.py`, directly below `supports_live_view` (the file already imports `settings`):

```python
    async def resolve_stream_transport(
        self, *, browser_session_id: str | None, organization_id: str | None
    ) -> str:
        """Which live-view transport serves this session: "vnc" or "cdp".

        A self-hosted deployment streams every browser the same way, so the
        deployment-wide setting decides.
        """
        return settings.BROWSER_STREAMING_MODE
```

- [ ] **Step 4: Implement the helpers in verify.py**

In `skyvern/forge/sdk/routes/streaming/verify.py` (module already imports `app`, `settings`, `LOG`):

```python
async def stream_transport(browser_session_id: str | None, organization_id: str) -> str:
    try:
        return await app.AGENT_FUNCTION.resolve_stream_transport(
            browser_session_id=browser_session_id, organization_id=organization_id
        )
    except Exception:
        LOG.warning(
            "Could not resolve the stream transport; using the deployment default",
            browser_session_id=browser_session_id,
            organization_id=organization_id,
            exc_info=True,
        )
        return settings.BROWSER_STREAMING_MODE


async def browser_session_id_for_runnable(runnable_id: str, organization_id: str) -> str | None:
    try:
        session = await app.PERSISTENT_SESSIONS_MANAGER.get_session_by_runnable_id(
            organization_id=organization_id, runnable_id=runnable_id
        )
    except Exception:
        LOG.warning(
            "Could not resolve a browser session for this runnable",
            runnable_id=runnable_id,
            organization_id=organization_id,
            exc_info=True,
        )
        return None
    return session.persistent_browser_session_id if session else None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/forge/sdk/routes/streaming/test_stream_transport.py -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Compile check and commit**

```bash
uv run python -m py_compile skyvern/forge/agent_functions.py skyvern/forge/sdk/routes/streaming/verify.py
git add skyvern/forge/agent_functions.py skyvern/forge/sdk/routes/streaming/verify.py \
    tests/unit/forge/sdk/routes/streaming/test_stream_transport.py
git commit -m "feat(SKY-13291): add per-session stream-transport resolution hook"
```

---

### Task 2: Per-session transport at the four serving-path gates

**Files:**
- Modify: `skyvern/forge/sdk/routes/streaming/screenshot.py:68` (task stream), `:191` (workflow-run stream), `:339` (browser-session stream)
- Modify: `skyvern/forge/sdk/routes/streaming/verify.py:94` (address-readiness bypass inside `verify_browser_session`)
- Test: `tests/unit/forge/sdk/routes/streaming/test_verify_browser_session.py` (extend)

**Interfaces:**
- Consumes: `verify.stream_transport(...)` and `verify.browser_session_id_for_runnable(...)` from Task 1; existing `_local_screencast_for_task/_workflow_run/_browser_session` helpers in `screenshot.py`.
- Produces: no new symbols — behavior change only. After this task, a deployment with `BROWSER_STREAMING_MODE=vnc` serves the CDP screencast to any session whose `resolve_stream_transport` answers `"cdp"`, and vice versa.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/forge/sdk/routes/streaming/test_verify_browser_session.py` (match its existing fixtures for building a ready session; the essential assertions):

```python
@pytest.mark.asyncio
async def test_verify_browser_session_cdp_transport_bypasses_missing_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session whose transport is cdp verifies without a browser address; a vnc session still requires one."""
    session = _make_session(is_browser_ready=False, browser_address=None)  # reuse this file's session factory
    manager = AsyncMock(get_session=AsyncMock(return_value=session))
    monkeypatch.setattr(app, "PERSISTENT_SESSIONS_MANAGER", manager)
    monkeypatch.setattr(app, "AGENT_FUNCTION", AsyncMock(resolve_stream_transport=AsyncMock(return_value="cdp")))
    monkeypatch.setattr(verify.settings, "BROWSER_STREAMING_MODE", "vnc")

    result = await verify.verify_browser_session("pbs_1", "org_1")
    assert result is not None
    assert result.browser_address == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/forge/sdk/routes/streaming/test_verify_browser_session.py -v -k cdp_transport`
Expected: FAIL — with the global set to `vnc`, the current code takes the address-lookup branch and returns `None`.

- [ ] **Step 3: Swap the verify.py bypass**

In `verify_browser_session` (`verify.py:94`), replace:

```python
    if not browser_address:
        if settings.BROWSER_STREAMING_MODE == "cdp":
            browser_address = ""
```

with:

```python
    if not browser_address:
        if await stream_transport(browser_session_id, organization_id) == "cdp":
            browser_address = ""
```

- [ ] **Step 4: Swap the three screenshot.py gates**

`screenshot.py` already imports from `verify`; extend that import with `stream_transport` and `browser_session_id_for_runnable`.

Task stream (line 68):

```python
    task_session_id = await browser_session_id_for_runnable(task_id, organization_id)
    if await stream_transport(task_session_id, organization_id) == "cdp":
        await _local_screencast_for_task(websocket, task_id, organization_id)
        return
```

Workflow-run stream (line 191):

```python
    run_session_id = await browser_session_id_for_runnable(workflow_run_id, organization_id)
    if await stream_transport(run_session_id, organization_id) == "cdp":
        await _local_screencast_for_workflow_run(websocket, workflow_run_id, organization_id)
        return
```

Browser-session stream (line 339):

```python
    if await stream_transport(browser_session_id, organization_id) == "cdp":
        await _local_screencast_for_browser_session(websocket, browser_session_id, organization_id)
        return
```

Leave `settings.BROWSER_STREAMING_MODE` reads outside the serving path untouched (`runtime_config.py`, `forge_app.py`, `default_persistent_sessions_manager.py`, CLI).

- [ ] **Step 5: Run the streaming suites**

Run: `uv run pytest tests/unit/forge/sdk/routes/streaming/ tests/unit_tests/test_streaming_screencast.py -v`
Expected: PASS (new test green, no regressions — existing tests exercising the old gates keep passing because the base hook returns the same setting).

- [ ] **Step 6: Compile check and commit**

```bash
uv run python -m py_compile skyvern/forge/sdk/routes/streaming/screenshot.py skyvern/forge/sdk/routes/streaming/verify.py
git add skyvern/forge/sdk/routes/streaming/screenshot.py skyvern/forge/sdk/routes/streaming/verify.py \
    tests/unit/forge/sdk/routes/streaming/test_verify_browser_session.py
git commit -m "feat(SKY-13291): gate stream endpoints on per-session transport"
```

---

### Task 3: Screencast browser-state fallback through the persistent-session manager

**Files:**
- Modify: `skyvern/forge/sdk/routes/streaming/screencast.py:53-65` (`_resolve_browser_state`)
- Test: `tests/unit_tests/test_streaming_screencast.py` (extend — this is the module's existing test file)

**Interfaces:**
- Consumes: `app.BROWSER_MANAGER.get_for_workflow_run(entity_id)` / `.get_for_task(entity_id, workflow_run_id)` (in-process, sync); `app.PERSISTENT_SESSIONS_MANAGER.get_session_by_runnable_id(...)` and `.get_browser_state(session_id, organization_id)` (async, does `connect_over_cdp` on the routed address in cloud).
- Produces: `_resolve_browser_state` now returns a `BrowserState` for task/workflow-run entities whose browser lives behind a persistent session even when this process holds no in-process state (the cloud API server case). `wait_for_browser_state`, the screencast loop, and `cdp_input.py` (which imports `_resolve_working_page`) inherit the behavior unchanged.

**Why:** the run/task stream endpoints run on the API server; in cloud, the in-process `BROWSER_MANAGER` belongs to the temporal worker, so `get_for_workflow_run`/`get_for_task` return `None` there. The run's browser is still reachable: entity → persistent session → `get_browser_state` (remote CDP).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit_tests/test_streaming_screencast.py` (reuse its fake-browser-state helpers; `@pytest.mark.asyncio` is this file's convention):

```python
@pytest.mark.asyncio
async def test_resolve_workflow_run_falls_back_to_persistent_session(monkeypatch: pytest.MonkeyPatch) -> None:
    state = object()
    session = AsyncMock()
    session.persistent_browser_session_id = "pbs_7"
    monkeypatch.setattr(app, "BROWSER_MANAGER", MagicMock(get_for_workflow_run=MagicMock(return_value=None)))
    monkeypatch.setattr(
        app,
        "PERSISTENT_SESSIONS_MANAGER",
        AsyncMock(
            get_session_by_runnable_id=AsyncMock(return_value=session),
            get_browser_state=AsyncMock(return_value=state),
        ),
    )

    resolved = await screencast._resolve_browser_state("wr_1", "workflow_run", organization_id="org_1")
    assert resolved is state


@pytest.mark.asyncio
async def test_resolve_workflow_run_without_session_or_org_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "BROWSER_MANAGER", MagicMock(get_for_workflow_run=MagicMock(return_value=None)))
    monkeypatch.setattr(
        app, "PERSISTENT_SESSIONS_MANAGER", AsyncMock(get_session_by_runnable_id=AsyncMock(return_value=None))
    )

    assert await screencast._resolve_browser_state("wr_1", "workflow_run", organization_id="org_1") is None
    assert await screencast._resolve_browser_state("wr_1", "workflow_run", organization_id=None) is None
```

Guard against `MagicMock` auto-attributes making assertions vacuous: assert identity (`is state`), not truthiness.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit_tests/test_streaming_screencast.py -v -k persistent_session`
Expected: FAIL — current code returns whatever `BROWSER_MANAGER` returned (`None`) with no fallback.

- [ ] **Step 3: Implement the fallback**

Replace `_resolve_browser_state` in `screencast.py`:

```python
async def _resolve_browser_state(
    entity_id: str,
    entity_type: str,
    workflow_run_id: str | None = None,
    organization_id: str | None = None,
) -> BrowserState | None:
    if entity_type == "workflow_run":
        state = app.BROWSER_MANAGER.get_for_workflow_run(entity_id)
        return state if state is not None else await _state_via_persistent_session(entity_id, organization_id)
    if entity_type == "task":
        state = app.BROWSER_MANAGER.get_for_task(entity_id, workflow_run_id)
        return state if state is not None else await _state_via_persistent_session(entity_id, organization_id)
    if entity_type == "browser_session":
        return await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(entity_id, organization_id)
    return None


async def _state_via_persistent_session(runnable_id: str, organization_id: str | None) -> BrowserState | None:
    """The runnable's browser may live behind a persistent session another process owns; reach it remotely."""
    if not organization_id:
        return None
    try:
        session = await app.PERSISTENT_SESSIONS_MANAGER.get_session_by_runnable_id(
            organization_id=organization_id, runnable_id=runnable_id
        )
        if not session:
            return None
        return await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
            session.persistent_browser_session_id, organization_id
        )
    except Exception:
        LOG.debug("Could not resolve browser state via persistent session", runnable_id=runnable_id, exc_info=True)
        return None
```

- [ ] **Step 4: Run the full screencast suite**

Run: `uv run pytest tests/unit_tests/test_streaming_screencast.py tests/unit/forge/sdk/routes/streaming/ -v`
Expected: PASS — including the SKY-11215 rebind tests (the fallback only engages when the manager returns `None`, so the resolve-each-poll behavior is unchanged).

- [ ] **Step 5: Compile check and commit**

```bash
uv run python -m py_compile skyvern/forge/sdk/routes/streaming/screencast.py
git add skyvern/forge/sdk/routes/streaming/screencast.py tests/unit_tests/test_streaming_screencast.py
git commit -m "feat(SKY-13291): resolve screencast browser state via persistent sessions when out-of-process"
```

---

### Task 4: `stream_transport` on `BrowserSessionResponse`

**Files:**
- Modify: `skyvern/webeye/schemas.py` (field near `vnc_streaming_supported` at line 76; factory `from_browser_session` near line 176)
- Test: `tests/unit/webeye/test_browser_session_response.py` (extend)

**Interfaces:**
- Consumes: `app.AGENT_FUNCTION.resolve_stream_transport(...)` from Task 1.
- Produces: `BrowserSessionResponse.stream_transport: str | None` — the value the frontend reads in Task 6. `"vnc"` or `"cdp"`; `None` only for old serialized payloads.

**Note:** additive optional field ⇒ Fern SDK drift. Follow `cloud_docs/fern-sdk/` regeneration policy in the PR (drift check is advisory; call the field out in the PR body).

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/webeye/test_browser_session_response.py` (reuse its session/factory fixtures):

```python
@pytest.mark.asyncio
async def test_response_carries_stream_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app, "AGENT_FUNCTION", AsyncMock(resolve_stream_transport=AsyncMock(return_value="cdp"), **_agent_function_defaults())
    )
    response = await BrowserSessionResponse.from_browser_session(_make_browser_session())
    assert response.stream_transport == "cdp"
```

Where `_agent_function_defaults()` supplies whatever other AgentFunction methods this factory already calls (`resolve_browser_session_connect_url`, `supports_live_view`, …) — reuse the file's existing stubbing pattern rather than inventing a new one.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/webeye/test_browser_session_response.py -v -k stream_transport`
Expected: FAIL — `stream_transport` not a field.

- [ ] **Step 3: Add the field and populate it**

Field (next to `vnc_streaming_supported`, line ~76):

```python
    stream_transport: str | None = Field(
        None,
        description='Live-view transport for this session: "vnc" or "cdp".',
        examples=["vnc", "cdp"],
    )
```

In `from_browser_session`, alongside the `vnc_streaming_supported=` argument:

```python
            stream_transport=await app.AGENT_FUNCTION.resolve_stream_transport(
                browser_session_id=browser_session.persistent_browser_session_id,
                organization_id=browser_session.organization_id,
            ),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/webeye/test_browser_session_response.py tests/cloud/test_vendor_identity_nondisclosure.py -v`
Expected: PASS — including the non-disclosure suite: the field carries only a neutral transport word, never a vendor identity or URL.

- [ ] **Step 5: Compile check and commit**

```bash
uv run python -m py_compile skyvern/webeye/schemas.py
git add skyvern/webeye/schemas.py tests/unit/webeye/test_browser_session_response.py
git commit -m "feat(SKY-13291): expose per-session stream transport on browser session responses"
```

---

### Task 5: Cloud override — vendor-held sessions stream via CDP

**Files:**
- Modify: `cloud/agent_functions.py` (next to `supports_live_view`, ~line 868)
- Test: `tests/cloud/test_stream_transport_resolution.py` (new — per-function cloud test files are this repo's pattern, cf. `test_browser_session_connect_url.py`)

**Interfaces:**
- Consumes: `cloud_db.browser_session_infra.get_browser_session_infra(persistent_browser_session_id) -> SessionInfra` (missing row ⇒ `FIRST_PARTY`; `SessionInfra.is_vendor: bool`). Both already imported/used in this module by `supports_live_view`.
- Produces: cloud `resolve_stream_transport` returning `"cdp"` for vendor rows, else `settings.BROWSER_STREAMING_MODE`.

- [ ] **Step 1: Write the failing tests**

Create `tests/cloud/test_stream_transport_resolution.py` (use the same infra-row fixtures as `tests/cloud/test_browser_session_infra.py` / the stubbing style of `test_browser_session_connect_url.py`):

```python
from unittest.mock import AsyncMock

import pytest

from cloud import agent_functions as cloud_agent_functions
from cloud.db.repositories.browser_session_infra import FIRST_PARTY, SessionInfra
from cloud.webeye.browser_types import BrowserInfraProvider


@pytest.mark.asyncio
async def test_vendor_session_streams_cdp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cloud_agent_functions.cloud_db.browser_session_infra,
        "get_browser_session_infra",
        AsyncMock(return_value=SessionInfra(provider=EXTERNAL_PROVIDER)),
    )
    fn = cloud_agent_functions.CloudAgentFunction()
    assert await fn.resolve_stream_transport(browser_session_id="pbs_1", organization_id="org_1") == "cdp"


@pytest.mark.asyncio
async def test_first_party_session_uses_deployment_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cloud_agent_functions.cloud_db.browser_session_infra,
        "get_browser_session_infra",
        AsyncMock(return_value=FIRST_PARTY),
    )
    monkeypatch.setattr(cloud_agent_functions.settings, "BROWSER_STREAMING_MODE", "vnc")
    fn = cloud_agent_functions.CloudAgentFunction()
    assert await fn.resolve_stream_transport(browser_session_id="pbs_1", organization_id="org_1") == "vnc"


@pytest.mark.asyncio
async def test_infra_lookup_failure_falls_back_to_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cloud_agent_functions.cloud_db.browser_session_infra,
        "get_browser_session_infra",
        AsyncMock(side_effect=RuntimeError("db down")),
    )
    monkeypatch.setattr(cloud_agent_functions.settings, "BROWSER_STREAMING_MODE", "vnc")
    fn = cloud_agent_functions.CloudAgentFunction()
    assert await fn.resolve_stream_transport(browser_session_id="pbs_1", organization_id="org_1") == "vnc"


@pytest.mark.asyncio
async def test_no_session_id_uses_deployment_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cloud_agent_functions.settings, "BROWSER_STREAMING_MODE", "vnc")
    fn = cloud_agent_functions.CloudAgentFunction()
    assert await fn.resolve_stream_transport(browser_session_id=None, organization_id="org_1") == "vnc"
```

Adjust the class name if the override class in `cloud/agent_functions.py` is not `CloudAgentFunction` — use whatever class holds `supports_live_view`, and construct it the way `test_browser_session_connect_url.py` does.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/cloud/test_stream_transport_resolution.py -v`
Expected: FAIL — the override does not exist, so the base returns the setting and the vendor test asserts `"cdp"` ≠ `"vnc"`.

- [ ] **Step 3: Implement the override**

In `cloud/agent_functions.py`, directly below `supports_live_view`:

```python
    async def resolve_stream_transport(
        self, *, browser_session_id: str | None, organization_id: str | None
    ) -> str:
        """A vendor-held browser has no websockify sidecar to relay RFB from, but its CDP endpoint
        is reachable through the session router — so it streams via CDP screencast. First-party
        sessions keep the deployment default."""
        if browser_session_id:
            try:
                infra = await cloud_db.browser_session_infra.get_browser_session_infra(browser_session_id)
            except Exception:
                LOG.warning(
                    "Could not resolve session infrastructure for stream transport; using the deployment default",
                    browser_session_id=browser_session_id,
                    exc_info=True,
                )
                return settings.BROWSER_STREAMING_MODE
            if infra.is_vendor:
                return "cdp"
        return settings.BROWSER_STREAMING_MODE
```

- [ ] **Step 4: Run the cloud suites**

Run: `uv run pytest tests/cloud/test_stream_transport_resolution.py tests/cloud/test_vendor_identity_nondisclosure.py tests/cloud/test_browser_session_infra.py -v`
Expected: PASS.

- [ ] **Step 5: Compile check and commit**

```bash
uv run python -m py_compile cloud/agent_functions.py
git add cloud/agent_functions.py tests/cloud/test_stream_transport_resolution.py
git commit -m "feat(SKY-13291): stream vendor-held sessions via CDP screencast transport"
```

---

### Task 6: Frontend — session type, transport resolver, and hook

**Files:**
- Modify: `skyvern-frontend/src/routes/workflows/types/browserSessionTypes.ts` (line ~18, next to `vnc_streaming_supported`)
- Modify: `skyvern-frontend/src/hooks/useRuntimeConfig.ts`
- Test: `skyvern-frontend/src/hooks/useRuntimeConfig.test.ts` (extend)

**Interfaces:**
- Consumes: `BrowserSessionResponse.stream_transport` from Task 4; existing `normalizeBrowserStreamingMode`, `useBrowserStreamingMode`, `getClient`, `useCredentialGetter`.
- Produces (Task 7 relies on these exact names):
  - `resolveStreamTransport(globalMode: BrowserStreamingMode, sessionTransport: string | null | undefined): BrowserStreamingMode` (pure, exported)
  - `useStreamTransport(browserSessionId?: string | null): { streamTransport: BrowserStreamingMode }` (exported hook; falls back to the global mode while loading or when the id is absent)
  - `BrowserSessionType.stream_transport?: string | null`

- [ ] **Step 1: Write the failing test**

Append to `skyvern-frontend/src/hooks/useRuntimeConfig.test.ts`:

```ts
import { resolveStreamTransport } from "./useRuntimeConfig";

describe("resolveStreamTransport", () => {
  it("prefers the session's transport over the global mode", () => {
    expect(resolveStreamTransport("vnc", "cdp")).toBe("cdp");
    expect(resolveStreamTransport("cdp", "vnc")).toBe("vnc");
  });

  it("falls back to the global mode when the session gives none or garbage", () => {
    expect(resolveStreamTransport("vnc", null)).toBe("vnc");
    expect(resolveStreamTransport("vnc", undefined)).toBe("vnc");
    expect(resolveStreamTransport("cdp", "")).toBe("cdp");
    expect(resolveStreamTransport("vnc", "webrtc")).toBe("vnc");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd skyvern-frontend && npx vitest run src/hooks/useRuntimeConfig.test.ts`
Expected: FAIL — `resolveStreamTransport` is not exported.

- [ ] **Step 3: Implement type, resolver, and hook**

`browserSessionTypes.ts` — add below `vnc_streaming_supported`:

```ts
  stream_transport?: string | null;
```

`useRuntimeConfig.ts` — add (and export) below `useBrowserStreamingMode`; add `import { useCredentialGetter } from "@/hooks/useCredentialGetter";` (same import path `BrowserSession.tsx` uses):

```ts
function resolveStreamTransport(
  globalMode: BrowserStreamingMode,
  sessionTransport: string | null | undefined,
): BrowserStreamingMode {
  if (!sessionTransport || !STREAMING_MODES.has(sessionTransport.trim().toLowerCase())) {
    return globalMode;
  }
  return normalizeBrowserStreamingMode(sessionTransport);
}

function useStreamTransport(browserSessionId?: string | null) {
  const { browserStreamingMode } = useBrowserStreamingMode();
  const credentialGetter = useCredentialGetter();
  const query = useQuery<{ stream_transport?: string | null }>({
    queryKey: ["browserSession", browserSessionId],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .get(`/browser_sessions/${browserSessionId}`)
        .then((response) => response.data);
    },
    enabled: Boolean(browserSessionId),
    staleTime: 5 * 60 * 1000,
    refetchOnWindowFocus: false,
    retry: 1,
  });

  return {
    streamTransport: resolveStreamTransport(
      browserStreamingMode,
      query.data?.stream_transport,
    ),
  };
}
```

Add both to the file's export list. Note: the `["browserSession", id]` queryKey deliberately matches `BrowserSession.tsx`'s existing session query so react-query dedupes the fetch on pages that already load the session.

- [ ] **Step 4: Run tests and type check**

Run: `cd skyvern-frontend && npx vitest run src/hooks/useRuntimeConfig.test.ts && npx tsc --noEmit`
Expected: PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add skyvern-frontend/src/hooks/useRuntimeConfig.ts skyvern-frontend/src/hooks/useRuntimeConfig.test.ts \
    skyvern-frontend/src/routes/workflows/types/browserSessionTypes.ts
git commit -m "feat(SKY-13291): per-session stream transport resolver and hook"
```

---

### Task 7: Frontend — switch the stream-selection call sites

**Files (all Modify):**
- `skyvern-frontend/src/routes/browserSessions/BrowserSession.tsx` (~line 51)
- `skyvern-frontend/src/routes/workflows/studio/StreamPresenter.tsx` (~line 30)
- `skyvern-frontend/src/routes/workflows/studio/runview/RunLiveStream.tsx` (~line 26)
- `skyvern-frontend/src/routes/tasks/detail/TaskActions.tsx` (~line 130)
- `skyvern-frontend/src/routes/workflows/workflowRun/WorkflowRunOverview.tsx` (~lines 51, 123)
- `skyvern-frontend/src/routes/workflows/editor/Workspace.tsx` (~lines 532, 582–585)

**Interfaces:**
- Consumes: `useStreamTransport(browserSessionId)` and `resolveStreamTransport(...)` from Task 6.
- Produces: no new symbols. `BrowserPaneHeader.tsx`'s `StreamModeBadge` intentionally keeps the global mode — it is a DEV-only diagnostic of deployment config, not a per-session control.

Per-site edits (each is: swap the hook, keep the rest of the expression identical):

- [ ] **Step 1: BrowserSession.tsx** — it already fetches the full session object, so use the pure resolver, not the fetching hook. Replace lines 51–52:

```tsx
  const { browserStreamingMode } = useBrowserStreamingMode();
  const isCdpMode = browserStreamingMode === "cdp";
```

with (the `browserSession` query result already exists below; move this derivation after it):

```tsx
  const { browserStreamingMode } = useBrowserStreamingMode();
  const isCdpMode =
    resolveStreamTransport(browserStreamingMode, browserSession?.stream_transport) === "cdp";
```

- [ ] **Step 2: StreamPresenter.tsx** — replace lines 30–31:

```tsx
  const { browserStreamingMode } = useBrowserStreamingMode();
  const useCdp = browserStreamingMode === "cdp" && !isRecording;
```

with:

```tsx
  const { streamTransport } = useStreamTransport(browserSessionId);
  const useCdp = streamTransport === "cdp" && !isRecording;
```

- [ ] **Step 3: RunLiveStream.tsx** — replace lines 26 and 34:

```tsx
  const { streamTransport } = useStreamTransport(browserSessionId);
  ...
  const useVnc =
    Boolean(browserSessionId) && streamTransport !== "cdp" && !vncFailed;
```

- [ ] **Step 4: TaskActions.tsx** — replace lines 130–131 (note `browserSessionId` is declared on line 129, before the current hook call):

```tsx
  const { streamTransport } = useStreamTransport(browserSessionId);
  const shouldUseCdpStream = streamTransport === "cdp";
```

- [ ] **Step 5: WorkflowRunOverview.tsx** — line 62 already has `const browserSessionId = workflowRun?.browser_session_id;`. Replace line 51's hook and line 123:

```tsx
  const { streamTransport } = useStreamTransport(browserSessionId);
  ...
  const shouldUseCdpStream = streamTransport === "cdp";
```

Move the hook call below the `workflowRun` query so `browserSessionId` is in scope (hooks order stays unconditional).

- [ ] **Step 6: Workspace.tsx** — the active debug session provides the id. Replace line 532's hook and lines 581–585:

```tsx
  const { streamTransport } = useStreamTransport(activeDebugSession?.browser_session_id);
  ...
  const isCdpStreamingMode = streamTransport === "cdp" && !recordingStore.isRecording;
  // Record Browser exfiltration requires VNC even when the transport is CDP streaming.
  const preferVncStream = streamTransport !== "cdp" || recordingStore.isRecording;
```

If `activeDebugSession` is declared after line 532, move the hook call to just after that declaration (unconditional call order preserved). Update the comment at line ~2547 (`mode comes from BROWSER_STREAMING_MODE / runtime config`) to `mode comes from the session's stream transport, falling back to runtime config`.

- [ ] **Step 7: Type check, lint, and grep for leftovers**

```bash
cd skyvern-frontend && npx tsc --noEmit && npx eslint \
  src/routes/browserSessions/BrowserSession.tsx src/routes/workflows/studio/StreamPresenter.tsx \
  src/routes/workflows/studio/runview/RunLiveStream.tsx src/routes/tasks/detail/TaskActions.tsx \
  src/routes/workflows/workflowRun/WorkflowRunOverview.tsx src/routes/workflows/editor/Workspace.tsx
grep -rn "useBrowserStreamingMode" src/routes | grep -v BrowserPaneHeader
```

Expected: clean; the grep returns only `BrowserSession.tsx` (resolver pattern keeps the global hook there) and nothing else outside the intentionally-global badge.

- [ ] **Step 8: Commit**

```bash
git add skyvern-frontend/src/routes/browserSessions/BrowserSession.tsx \
    skyvern-frontend/src/routes/workflows/studio/StreamPresenter.tsx \
    skyvern-frontend/src/routes/workflows/studio/runview/RunLiveStream.tsx \
    skyvern-frontend/src/routes/tasks/detail/TaskActions.tsx \
    skyvern-frontend/src/routes/workflows/workflowRun/WorkflowRunOverview.tsx \
    skyvern-frontend/src/routes/workflows/editor/Workspace.tsx
git commit -m "feat(SKY-13291): select live-stream component per session transport"
```

---

### Task 8: Capability flip — vendors serve live view (rollout switch, own PR)

**Files:**
- Modify: `cloud/webeye/browser_types.py` (`PROVIDER_CAPABILITIES`, ~lines 133–145, and the `live_view` comment ~line 121)
- Test: `tests/cloud/test_pbs_infra_routing.py` (extend/adjust)

**Interfaces:**
- Consumes: everything above — this is the switch that routes `needs_live_view=True` sessions to vendors AND (through the existing cloud `supports_live_view`, which returns `capabilities_for(infra.provider).live_view`) makes `vnc_streaming_supported=True` on vendor session responses.
- Produces: `live_view is True` in `PROVIDER_CAPABILITIES` for every external provider.

**Do not merge until Tasks 1–7 are deployed and a staging vendor session shows a working stream.** Reverting this one PR restores the pre-feature routing exactly.

- [ ] **Step 1: Write/adjust the failing tests**

In `tests/cloud/test_pbs_infra_routing.py`, find the test asserting `needs_live_view` forces first-party (capability override) and split the assertion:

```python
@pytest.mark.asyncio
async def test_needs_live_view_no_longer_forces_first_party() -> None:
    for provider in EXTERNAL_PROVIDERS:
        assert capabilities_for(provider).live_view is True
```

Also update (do not delete) any existing test that pinned `live_view=False` — its name documents the old deliberate design; rewrite it to pin the new one.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/cloud/test_pbs_infra_routing.py -v`
Expected: FAIL on the new assertion.

- [ ] **Step 3: Flip the table**

In `PROVIDER_CAPABILITIES`, set `live_view=True` for every external provider entry. Replace the `live_view` field comment (lines ~118–121) with:

```python
    # First-party sessions stream RFB from the pod's websockify sidecar; vendor-held sessions
    # stream CDP screencast through the session router (resolve_stream_transport decides per row).
    live_view: bool
```

- [ ] **Step 4: Run the cloud suites**

Run: `uv run pytest tests/cloud/test_pbs_infra_routing.py tests/cloud/test_stream_transport_resolution.py tests/cloud/test_pbs_vendor_session_lifecycle.py -v`
Expected: PASS.

- [ ] **Step 5: Compile check and commit**

```bash
uv run python -m py_compile cloud/webeye/browser_types.py
git add cloud/webeye/browser_types.py tests/cloud/test_pbs_infra_routing.py
git commit -m "feat(SKY-13291): vendors serve live view via CDP screencast transport"
```

---

## End-to-end verification (before the Task 8 PR merges)

1. Backend: `uv run pytest tests/unit/forge/sdk/routes/streaming/ tests/unit_tests/test_streaming_screencast.py tests/unit/webeye/test_browser_session_response.py tests/cloud/ -q` and `uv run alembic check` (no schema change expected).
2. Local smoke (OSS mode unchanged): `BROWSER_STREAMING_MODE=cdp ./run_skyvern.sh`, open a browser session page, confirm the screencast still streams (base hook returns the global setting — behavior identical to today).
3. Staging smoke (the new path): create a vendor-routed session via the API (`DYNAMIC_BROWSER_TYPE` flag arm or the infra flag), GET `/browser_sessions/{id}` → `stream_transport == "cdp"` and `vnc_streaming_supported == true` (after Task 8), open the session page → `InteractiveStreamView` renders frames. Capture before/after screenshots for the FE PR.
4. Watch router load during the staging soak: screencast frames traverse the cdp-proxy (`skyvern.cdp_proxy.*` metrics); SKY-12502 (frame throttle/downscale via the policy engine) is the companion if per-viewer byte rates need bounding.

## Known risks

- `useStreamTransport` adds one `/browser_sessions/{id}` fetch on run pages that didn't previously load the session (deduped elsewhere via the shared queryKey). Acceptable; revisit only if endpoint latency shows up.
- The vendor's own operator UI could theoretically hold a screencast on the same page target; the spike saw no interference, but if frames stall while automation runs, check for a competing `Page.startScreencast` client before blaming the router.
- One provider's `cdp_url` is `http(s)` requiring `/json/version` discovery — irrelevant here (Playwright's `connect_over_cdp` handles it, and the API server dials the routed `browser_address`), but any future raw-websocket consumer must replicate it.
