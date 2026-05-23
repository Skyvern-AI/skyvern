# Plan: Stagehand + Skyvern Integration

> **Plan revision:** rev3 — 2026-05-23.
> rev1 = original draft; rev2 = rewritten after deep codebase read; rev3 = adopted reviewer fixes (CDP URL discovery, screenshot-event mapping, multi-tab handling, sidecar lifecycle, fallback scope).
>
> **Note on terminology:** "rev1/rev2/rev3" refers to versions of *this plan document*. Where the plan talks about the integration shipping in stages, it says "first release" / "follow-up release" explicitly. `RunEngine.skyvern_v1` and `RunEngine.skyvern_v2` are pre-existing Skyvern engine identifiers (real product names), unrelated to plan revisions.

## Goal

Make Skyvern faster by routing the per-step action-generation work through Stagehand's TypeScript engine (CDP-direct, cached actions, self-healing) while **preserving Skyvern's full observability stack** — UI timeline, per-step screenshots, session recording, artifact storage, workflow orchestration, and DB state tracking.

Stagehand is added as a new `RunEngine.stagehand` alongside `skyvern_v1`, `skyvern_v2`, `openai_cua`, `anthropic_cua`, `ui_tars`. Existing engines stay; users pick per task/workflow.

End state: a system that is **2-5x faster on cached runs** (and ~1.5-2x faster on cold runs from CDP-direct execution) while keeping the same user-facing experience in the Skyvern UI.

---

## Verified facts (replaces the v1 "what we don't know" section)

### Stagehand side
- **CDP attach is fully supported.** `packages/core/lib/v3/v3.ts:906` — when `localBrowserLaunchOptions.cdpUrl` is set, V3 calls `V3Context.create(lbo.cdpUrl, …)` instead of launching its own browser. This is the load-bearing primitive for the co-tenant architecture below.
- **Event bus exists and is rich.** `packages/core/lib/v3/flowlogger/FlowLogger.ts:36` defines `FlowEvent { eventType, eventId, eventParentIds, eventCreatedAt, sessionId, data }`. V3 registers `this.bus.on("*", this.eventStore.emit)` (`v3.ts:428`) — wildcard subscription works. Event types in use include `StagehandAct`, `StagehandExtract`, `StagehandObserve`, `AgentExecute`, `LlmRequestEvent`, `LlmResponseEvent`, plus 20+ `Page*` events (`PageGoto`, `PageClick`, `PageType`, `PageScreenshot`, `PageScroll`, …). Each event is auto-suffixed with `StartEvent`/`CompletedEvent`/`ErrorEvent`, and parent/child relationships are tracked via `eventParentIds`.
- **server-v3 SSE already exists.** `packages/server-v3/src/lib/stream.ts` (221 lines) implements SSE for every route via `createStreamingResponse()`; clients opt in via the `shouldRespondWithSSE` header check. Framework is Fastify ^5.8.5.
- **server-v3 does NOT pipe bus events into SSE.** Grep of `packages/server-v3/src/` for `bus.` returns zero hits. Today's SSE carries lifecycle (`starting/connected/running/finished/error`) plus a generic `running/log` channel fed by `requestContext.logger(message: string)`. Forwarding `bus.on("*")` events through this channel is the actual missing work (~30 lines in `stream.ts`).
- **Python SDK is not an option for local execution.** `stagehand-python` is an HTTP client to `https://api.stagehand.browserbase.com` — it does not run Stagehand locally. The only ways to use stagehand locally from Python are (a) a Node sidecar or (b) port the TS engine. We are going with (a).

### Skyvern side
- **`RunEngine` enum already supports multiple engines** at `skyvern/schemas/run_enums.py:13`. Adding `stagehand = "stagehand"` is the natural insertion point — no new top-level config flag needed.
- **Per-engine branching is already centralized** in `skyvern/forge/agent.py:1407-1435` (inside `agent_step`). Today it branches `openai_cua`, `anthropic_cua`, `ui_tars`, with a default branch for `skyvern_v1`. We add an `elif engine == RunEngine.stagehand:` branch that delegates action generation + execution to the sidecar.
- **`BrowserState` is a clean 18-method Protocol** (`skyvern/webeye/browser_state.py:15`). It owns the Playwright `BrowserContext`, screenshots, scraping, navigation, and `BrowserArtifacts` (HAR, video, console log). Keeping Skyvern as browser owner means **none of these methods need to change**.
- **Skyvern's browser_factory already launches Chromium with `--remote-debugging-port=<cdp_port>`** (`skyvern/webeye/browser_factory.py:320-321`). The CDP endpoint Stagehand needs already exists; we just need to thread it through.
- **Block layer doesn't need changes.** `BaseTaskBlock.execute` (`skyvern/forge/sdk/workflow/models/block.py:1067`) calls `app.agent.execute_step(..., engine=engine, ...)` (cf. `agent.py:1339`). The engine is already a per-task parameter that cascades from the workflow definition.
- **ArtifactType enum** at `skyvern/forge/sdk/artifact/models.py:10` has 30+ entries. The ones the UI relies on most are `SCREENSHOT_LLM/ACTION/FINAL`, `LLM_REQUEST/RESPONSE`, `RECORDING`, `HAR`, `STEP_ARCHIVE`. `VISIBLE_ELEMENTS_TREE*` are Skyvern-scraper-specific and have **no equivalent in Stagehand's event model** — gracefully-degraded UI or parallel scraper required (see Risks).

### CDP co-tenancy: verified, narrow risk
Originally framed as the load-bearing unknown. After auditing both sides' actual CDP usage, the risk is much smaller than v1 suggested:

- **Multiple CDP clients on one Chromium**: natively supported (Chrome DevTools UI + Playwright do this routinely).
- **Stagehand v3 does NOT use Playwright** — it's a custom raw CDP client at `packages/core/lib/v3/understudy/cdp.ts`. This actually *removes* a class of conflict (no two Playwright instances fighting for the same context); each side speaks raw CDP over its own WebSocket.
- **`Network.*`**: Stagehand only calls `Network.enable` (`networkManager.ts:142`) — pure read-only event subscription. Both sides subscribe independently; both get every event. No conflict.
- **`Fetch.*`**: Skyvern uses `Fetch.enable` for download detection (`cdp_download_interceptor.py:336`). Stagehand never calls `Fetch.*` at all (grep-verified). No conflict.
- **`Browser.setDownloadBehavior`**: Both can call it, but Stagehand only does so when init options include `downloadsPath` or `acceptDownloads` (`v3.ts:1142`). The sidecar omits these — Skyvern keeps owning downloads. Avoidable by configuration.
- **Concurrent `Page.navigate`**: Avoided by orchestration. The agent loop is naturally phased: Stagehand acts → Skyvern screenshots → LLM decides next. Sequencing is enforced at the orchestration layer, not the protocol.
- **`Target.attachedToTarget` / new tabs**: Both sides auto-attach and create independent Page abstractions. CDP-level: no shared mutable state. **But there's a Skyvern-internal hazard:** Skyvern tracks an explicit "working page" (`real_browser_state.py:127, 175, 245` — `set_working_page()`) that determines which page screenshots come from. When Stagehand opens a new tab via its own CDP session, Skyvern's working-page reference goes stale and screenshots come from the wrong page. **Mitigation in the adapter:** after every `StagehandActCompletedEvent` and `PageGotoCompletedEvent`, the `StagehandEngine` calls `await browser_state.list_valid_pages()` and `set_working_page(pages[-1], len(pages)-1)` to mirror Skyvern's own last-page convention. ~10 LOC in `event_mapper.py`. Phase 1 spike must include a tab-opening scenario to confirm this works.

The remaining CDP-protocol edge worth verifying in the Phase 1 spike: Skyvern's `Fetch.enable` is per-target session; when Stagehand navigates, the new document may briefly process requests before Skyvern re-attaches `Fetch.enable` to the new frame. Skyvern already handles navigation today, so this likely works out — but worth a direct test.

### Architectural fit summary
Both codebases natively support every primitive this integration needs. The integration is mostly glue, not surgery.

---

## Architecture

**CDP co-tenant model.** Skyvern owns the browser process and the entire observability pipeline. Stagehand attaches over CDP and contributes only action decisions. The browser is the shared substrate; neither side rebuilds the other's stack.

```
┌────────────────────────────── Skyvern (Python) ─────────────────────────────┐
│                                                                             │
│  Workflow Engine                                                            │
│   └─► BaseTaskBlock.execute()  [unchanged]                                  │
│        └─► app.agent.execute_step(..., engine=RunEngine.stagehand)          │
│             └─► agent.agent_step()  [adds stagehand branch at L1407]        │
│                  └─► StagehandEngine.run_step(task, step, browser_state)    │
│                       │                                                     │
│  BrowserState (Protocol; owns Playwright + Chromium w/ --remote-debugging)  │
│   ├─► browser_artifacts.py  → RECORDING, HAR, CONSOLE_LOG  [unchanged]      │
│   ├─► take_*_screenshot()   → SCREENSHOT_ACTION/LLM/FINAL  [unchanged]      │
│   └─► scrape_website()      → VISIBLE_ELEMENTS_TREE*  [opt-in for parity]   │
│                                                                             │
│  StagehandEngine                ◄──── async event stream (SSE) ────┐        │
│   ├─► HTTP client to sidecar                                       │        │
│   ├─► FlowEvent → ArtifactType mapper                              │        │
│   └─► Writes Step rows, Action rows, Artifact rows  [DB unchanged] │        │
└────────────────────────────────────────────────────────────────────│────────┘
                                                                     │
                       CDP: ws://127.0.0.1:<cdp_port>                │ JSON / SSE
                                  ▲                                  │ over UDS or
                                  │                                  │ localhost HTTP
┌─────────────────────────── Node sidecar ───────────────────────────▼────────┐
│  Long-lived Fastify server (forked from packages/server-v3, ~80% reused)    │
│  Endpoints:                                                                 │
│    POST /v1/sessions/start          { cdpUrl } → sessionId                  │
│    POST /v1/sessions/:id/act        { instruction }                         │
│    POST /v1/sessions/:id/agentExecute { instructions, maxSteps }            │
│    POST /v1/sessions/:id/extract    { schema }                              │
│    POST /v1/sessions/:id/end                                                │
│  All endpoints stream FlowEvents back over SSE (existing infra + new wire). │
│                                                                             │
│  Depends on @browserbasehq/stagehand as an npm package (no fork of core).   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Why this shape

- **No fork of `@browserbasehq/stagehand`.** The sidecar depends on the upstream npm package. Future upstream improvements arrive via `npm update`.
- **Skyvern keeps owning the browser.** Recording, HAR, console log, screenshots all keep flowing through the existing `BrowserArtifacts` pipeline with zero rewiring. This is the single biggest win of the co-tenant model.
- **No new top-level concepts in Skyvern.** A new value in an existing enum, a new branch in an existing method, a new adapter file. The workflow engine, blocks, parameters, Bitwarden credentials, and per-block downloads are all untouched.
- **Per-engine selection is already a first-class concept** in Skyvern. We're filling a slot, not building one.

---

## Implementation

### A. Skyvern side (Python)

| File | Change | Estimated LOC |
|---|---|---|
| `skyvern/schemas/run_enums.py:13` | Add `stagehand = "stagehand"` to `RunEngine` | 1 |
| `skyvern/services/stagehand/__init__.py` | New package | — |
| `skyvern/services/stagehand/engine.py` | `class StagehandEngine`: sidecar lifecycle (spawn, health-check, shutdown), HTTP client, SSE consumer, action loop | ~400 |
| `skyvern/services/stagehand/event_mapper.py` | `FlowEvent` → Skyvern `Artifact` / `Action` / `Step` writes | ~300 |
| `skyvern/services/stagehand/session.py` | Per-task Stagehand session: CDP URL discovery from `BrowserState`, attach, teardown | ~150 |
| `skyvern/forge/agent.py:1407` | Add `elif engine == RunEngine.stagehand: actions = await self._generate_stagehand_actions(...)` | ~30 |
| `skyvern/forge/agent.py` (new method) | `_generate_stagehand_actions()` — thin wrapper that calls `StagehandEngine` and adapts the response back to Skyvern `Action` objects | ~80 |
| `skyvern/config.py` | `STAGEHAND_SIDECAR_PORT`, `STAGEHAND_SIDECAR_PATH`, `STAGEHAND_TIMEOUT_S` | ~10 |
| `skyvern/cli/commands/run.py` | Spawn sidecar as part of `skyvern run all` | ~30 |
| `tests/unit_tests/services/test_stagehand_engine.py` | Unit tests with mocked sidecar | ~300 |

**Total new Python: ~1300 LOC, with ~30 lines modified in `agent.py`.** Most of it is the event mapper — boring code with high test coverage. None of it touches `BrowserState`, the workflow engine, blocks, or the API surface.

### B. Sidecar (TypeScript)

Two viable shapes; recommendation is **(1)**:

**Option 1: Lightly modified fork of `packages/server-v3`.** Live as a sibling dir in this repo: `stagehand-sidecar/` (or inside `skyvern/stagehand-sidecar/`). Depends on `@browserbasehq/stagehand` as a workspace-external npm package. Changes vs. upstream:

| File | Change | Estimated LOC |
|---|---|---|
| `src/lib/stream.ts` | Subscribe to `stagehand.bus.on("*", emitToSSE)` after `getOrCreateStagehand`; unsubscribe on completion/error. Forward each `FlowEvent` as `sendData("running", "log", { kind: "flowEvent", event })`. | ~30 |
| `src/routes/v1/sessions/start.ts` | Accept `{ cdpUrl }` and pass through to `localBrowserLaunchOptions.cdpUrl` so `V3.init()` attaches instead of launching | ~15 |
| `src/lib/SessionStore.ts` | Tighten lifecycle: graceful teardown on Skyvern disconnect; emit a final `sessionEnd` event | ~20 |
| (rest of `server-v3`) | unchanged | 0 |

**Total new/modified TS: ~65 LOC on top of an unchanged upstream server.**

**Option 2: Thin custom sidecar against `packages/core`.** ~500 LOC, more code to maintain, but no Browserbase-specific session abstractions to work around. Defer unless option 1 hits friction.

### C. Event → Artifact mapping

| Stagehand `FlowEvent.eventType` | Skyvern artifact / row | Notes |
|---|---|---|
| `LlmRequestEvent` | `Artifact(ArtifactType.LLM_REQUEST)` + `Step.input_token_count++` | Payload has full prompt |
| `LlmResponseEvent` | `Artifact(ArtifactType.LLM_RESPONSE)` + `Step.output_token_count++` | Payload has full response, parsed tool calls |
| `StagehandActStartEvent` | (begin Action row) + `BrowserState.take_fullpage_screenshot()` → `SCREENSHOT_LLM` | Pre-action page state (what the model saw) |
| `StagehandActCompletedEvent` | Finalize `Action` row with status=success + `BrowserState.take_post_action_screenshot()` → `SCREENSHOT_ACTION` | Post-action screenshot per Skyvern's `SCREENSHOT_ACTION` convention |
| `StagehandActErrorEvent` | Finalize `Action` row with status=failed, store reason | |
| `StagehandExtractCompletedEvent` | `Artifact(ArtifactType.LLM_RESPONSE_PARSED)` with extracted JSON | |
| `AgentExecuteStartEvent` | Begin `Step` row | |
| `AgentExecuteCompletedEvent` | Finalize `Step` row, increment task step counter | |
| `PageGotoCompletedEvent` | `Action(action_type=NAVIGATE)` row | URL in payload |
| `PageClickCompletedEvent` | `Action(action_type=CLICK)` row | Selector + coordinates in payload |
| `PageTypeCompletedEvent` | `Action(action_type=INPUT_TEXT)` row | |
| `PageScreenshotCompletedEvent` | (skip — Skyvern's BrowserState handles screenshots directly) | Avoid double-capture |
| Other `Page*` events | Log only (debug level) | Not needed for UI |
| `*ErrorEvent` (any) | Annotate the parent Step/Action with failure reason via `eventParentIds` lookup | |
| (no equivalent) | `VISIBLE_ELEMENTS_TREE*` artifacts | **Lossy.** See Risks. |

### D. CDP wiring

1. Skyvern's `BrowserState.check_and_fix_state()` already launches Chromium with `--remote-debugging-port=<cdp_port>` (`browser_factory.py:320`). Today `cdp_port` is optional and rarely set; for stagehand engines we make it required (auto-assign if absent — pick a free port via `socket`).
2. **CDP WebSocket URL discovery — not a one-liner.** Skyvern launches via `launch_persistent_context()` (`browser_factory.py:613,632,702,721`), which returns a `BrowserContext` whose `.browser` attribute is `None` in Playwright's persistent-context model. Skyvern's own download interceptor confirms this — `enable_browser_download_monitor` takes `browser: Browser` as a *separate parameter* threaded from upstream (`cdp_download_interceptor.py:352`). Discovery mechanism: hit `http://127.0.0.1:<cdp_port>/json/version` and read `webSocketDebuggerUrl` (standard Chromium DevTools HTTP endpoint, always available when `--remote-debugging-port` is set). Add a `get_cdp_websocket_url(self) -> str` method to `BrowserState` that does this lookup (with retry, since the endpoint is briefly unavailable during browser startup). Estimated ~30 LOC including retry + timeout, not 5.
3. `StagehandEngine.run_step` calls `POST /v1/sessions/start { cdpUrl }` on the sidecar; sidecar calls `V3.init({ localBrowserLaunchOptions: { cdpUrl } })`.
4. Both clients hold separate CDP sessions on the same Chromium target. Playwright (Skyvern) does screenshots, navigation, file downloads, recording. Stagehand does action decisions and DOM interaction.

---

## Phased rollout

### Phase 1 — Verify CDP co-tenancy (~1 day)

The source analysis has already ruled out the obvious conflicts (see "CDP co-tenancy" above). This spike is a focused empirical verification, not the open-ended de-risking the v1 plan implied.

- Skyvern launches a browser with `cdp_port=9222`.
- A throwaway Node script calls `stagehand.init({ localBrowserLaunchOptions: { cdpUrl: "ws://127.0.0.1:9222/devtools/browser/..." } })` — explicitly omitting `downloadsPath` and `acceptDownloads` so Stagehand never touches `Browser.setDownloadBehavior`.
- Run a 5-min loop: Stagehand clicks/types/navigates while Skyvern continuously takes screenshots, reads DOM via its scraper, and exercises its `Fetch.enable`-based download interception.
- **Specifically test the one remaining edge:** Skyvern's `Fetch.enable` re-attach after a Stagehand-initiated navigation. Verify no requests slip through unintercepted.
- Watch for: `Target closed` errors, dropped CDP messages, screenshot races, frame detachment errors.

**Exit criteria:** Zero errors over 100 consecutive cycles on each of 3 representative sites (form fill, multi-page nav, file download).

If the spike surfaces a conflict not predicted by the source audit, escalate before committing to the rest of the plan — but at this point the architecture is unlikely to need rework.

### Phase 2 — Sidecar + bus-to-SSE wiring + lifecycle + distribution (5-7 days; expanded from rev2)

- Fork `packages/server-v3` into `stagehand-sidecar/`.
- Add the ~30-line bus-to-SSE subscription in `lib/stream.ts`.
- Add `{ cdpUrl }` plumbing to `start.ts`.
- **Lifecycle & supervision** (was hand-waved in rev2, broken out here per reviewer):
  - Startup race: `StagehandEngine.__init__` blocks on `GET /healthcheck` polling (250ms interval, 10s ceiling) before accepting any task. Sidecar binds the port only after `Stagehand` module is loadable.
  - Graceful shutdown: SIGTERM → drain in-flight sessions (server-v3 has the hook) → exit. Skyvern's `skyvern stop all` sends SIGTERM and waits ≤ 30s.
  - Crash recovery: supervisor in Python restarts sidecar up to 3× per minute; persistent failure marks the engine unhealthy and routes new tasks to fallback.
- **Concurrent session isolation** (was an unverified assumption in rev2): server-v3's `SessionStore` keys per-session state by `sessionId`, but `FlowLogger` uses `AsyncLocalStorage` for parent-event context (`FlowLogger.ts:100`). AsyncLocalStorage is per-async-chain not per-session — verify with a two-session interleaved test that events from session A never appear in session B's SSE stream. If they do, scope the bus subscription by `event.sessionId` filter.
- **Binary distribution** (was "Open Question 2" in rev2; promoted to blocking per reviewer): you cannot integration-test Phase 3 without a runnable sidecar binary. Decide here. Recommended: use stagehand's existing `build:sea:esm` script to produce a single-file executable, ship in the Skyvern wheel under `skyvern/bin/stagehand-sidecar`. Fallback if SEA proves brittle: ship the bundled JS + a vendored Node binary. Either way, decision and CI build step land in this phase.
- Write a Python `SidecarClient` (just HTTP + SSE consumer, no Skyvern integration yet) and a curl-equivalent integration test.
- **Exit criteria:**
  1. Python script drives a full `agentExecute` end-to-end and prints FlowEvents as they arrive.
  2. Two concurrent sessions against different CDP URLs run for 60s with zero cross-session event leakage.
  3. A built sidecar binary runs from a clean machine with no `npm install` step (or the documented fallback works).
  4. Kill -9 the sidecar mid-task → supervisor restarts it → next task succeeds.

### Phase 3 — `StagehandEngine` adapter + event mapper (5-7 days)

- Build `services/stagehand/{engine,session,event_mapper}.py` per the table above.
- Add the `RunEngine.stagehand` branch in `agent.py:1407`.
- Cover the mapper with unit tests (one test per row of the mapping table).
- **Exit criteria:** Run a single-step task with `engine=stagehand` and verify Step + Action + Artifact rows in the DB; screenshots and recording present.

### Phase 4 — Workflow integration (3-5 days)

- Verify multi-block workflows: state passes through normally because the engine is per-block and Skyvern's BrowserState persists across blocks in the same workflow run.
- Verify file downloads work (Skyvern's `CDPDownloadInterceptor` continues to operate).
- Verify Bitwarden credentials block still injects values (it runs before the agent step, not inside).
- **Exit criteria:** A real 3-block workflow runs end-to-end on stagehand engine and looks visually correct in the Skyvern UI.

### Phase 5 — Benchmarking + UI parity audit (2-3 days)

- Pick 5 representative tasks (one of which is the Google Drive → Gmail demo from v1). Run each 3× on `skyvern_v1` and 3× on `stagehand` (with empty cache, then again with warm cache).
- Capture: wall-clock duration, LLM cost, action count, success rate.
- Open the Skyvern UI on each stagehand run and audit: timeline complete? screenshots present? recording plays? action reasoning shown?
- **Exit criteria:** Stagehand engine matches success rate within ±5%; cold-run speedup ≥1.5x; warm-cache speedup ≥3x; no UI tab/panel is broken or empty.

### Phase 6 — Production gating (1 week)

- Feature flag: `STAGEHAND_ENGINE_ENABLED` (org-level), then per-workflow opt-in.
- Telemetry dashboard: per-engine success rate, p50/p95 duration, fallback rate.
- **Fallback model — all-or-nothing at task boundary** (revised from rev2 per reviewer): if Stagehand returns a known-recoverable error category (CAPTCHA detected, 2FA required, sidecar unhealthy), the task is marked failed with a `fallback_recommended=true` annotation; the orchestration layer can re-run it on `RunEngine.skyvern_v1`. **Mid-task engine switching is explicitly out of scope for the first release** — Stagehand's action concept and Skyvern's `Action` row don't share a state model, and reconciling partial completion (step `retry_index`, action history) mid-task is a separate design effort. Defer to a follow-up release if data shows it's needed.
- **Engine availability UX:** workflow definitions persist their `engine` value. If a saved workflow has `engine=stagehand` and the org's `STAGEHAND_ENGINE_ENABLED` is false (or the engine is unhealthy), the workflow run rejects at execution time with a clear "engine not available for this org" error visible in the UI. The workflow builder shows engine availability in the engine selector. No silent fallback.

---

## Risks & mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| **CDP co-tenancy unstable.** | Low (was High in v1; reduced after source audit). Specific conflicts (`Fetch.enable`, `Browser.setDownloadBehavior`) ruled out by inspection — Stagehand never touches `Fetch.*` and only sets download behavior when given explicit opts. | Phase 1 spike confirms empirically; sidecar config omits download options. |
| **`Fetch.enable` re-attach race after Stagehand navigation.** | Low-Medium | Verify in Phase 1 spike. Skyvern's existing navigation code path likely already handles this; if not, add a `Page.frameNavigated` listener that re-asserts the Fetch pattern. |
| **`VISIBLE_ELEMENTS_TREE*` artifacts missing → UI panels empty.** | Medium | Two-tier mitigation: (a) **first release**, accept the gap; audit which UI panels actually render without these and either hide them per-engine or show a "not captured for this engine" placeholder. (b) **follow-up release**, opt-in flag to run Skyvern's scraper in parallel for parity — costs ~30% of the speedup, but available when needed. |
| **Sidecar crashes mid-task.** | Medium | Supervisor in `StagehandEngine` restarts up to N times; on terminal failure, the task is marked `failed` with the sidecar's last error event. Skyvern's existing retry logic handles re-running. |
| **Browserbase upstream breaks the SSE wire format.** | Low-Medium | Pin `@browserbasehq/stagehand` to a specific minor version. Sidecar's bus-to-SSE bridge is tiny and easy to re-port across upstream changes. |
| **Captchas / 2FA / file uploads not handled by Stagehand.** | Medium | Per-engine task-boundary fallback (Phase 6). For the first release, document the limitations and require workflows using these patterns to stay on `RunEngine.skyvern_v1`. |
| **Cache staleness causes silent regressions.** | Low | Stagehand emits `ActErrorEvent` on cache miss + self-heal failure. Mapper routes these to a Skyvern `Action.status=failed` with reason="stagehand_cache_stale", visible in the UI. |
| **Stagehand cost ≠ Skyvern cost (different LLMs, different prompts).** | Low | Cost dashboard tracks per-engine separately; orgs can choose the cheaper engine per workflow class. |

---

## Open questions (smaller list after rev3 reviewer fixes)

Resolved in rev3 and moved into the body of the plan: CDP URL discovery (see "D. CDP wiring"), sidecar binary distribution (see "Phase 2"), multi-tab working-page sync (see "CDP co-tenancy" + event mapper).

Still open:

1. **Multi-tenant browser session sharing.** If a Skyvern workflow uses `browser_session_id` to share a browser across runs, can multiple stagehand sessions attach sequentially to the same CDP endpoint? Likely yes (CDP supports reconnect), but verify in Phase 4.
2. **CUA engines + Stagehand.** Should stagehand and the CUA engines (`openai_cua`, `anthropic_cua`, `ui_tars`) be combinable, or mutually exclusive? Recommend mutually exclusive for the first release — simpler.
3. **Action-row schema fidelity.** Stagehand emits richer event data (e.g. selector reasoning, self-heal attempts) than Skyvern's `Action` row has columns for. First release: drop into a generic `Action.metadata` JSON field. Long-term: extend the schema if the UI wants first-class display.

---

## Success criteria (unchanged from v1)

The integration is successful when:

1. **Same demo works**: Google Drive download → Gmail attach completes via `RunEngine.stagehand`
2. **UI shows same data**: Screenshots, timeline, recording, actions all visible in Skyvern UI
3. **Faster cold (measurement target, not commit)**: Task completes in < 70% of `skyvern_v1` time on first run. SSE + localhost HTTP + Python event processing add ~10-50ms per step; on a 5-15s/step loop that's noise, so 1.4×+ cold speedup is plausible, but verify in Phase 5 before treating as binding.
4. **Faster warm**: Second run of same task uses cached actions and completes in < 40% of `skyvern_v1` time
5. **No regressions**: Workflow engine, API, webhooks all work identically for non-stagehand engines
6. **Graceful degradation**: If Stagehand fails, system reports meaningful error or falls back to `skyvern_v1` based on per-org config

---

## Next step

Start Phase 1 — the CDP co-tenancy spike. The source audit has already ruled out the major conflict classes (see "CDP co-tenancy: verified, narrow risk"), so the spike is now a ~1 day empirical confirmation rather than the v1 "architecture-altering unknown." Once green, Phase 2 (sidecar + bus-to-SSE wiring) can begin immediately.
