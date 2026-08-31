"""Which browser one copilot tool call acts in, resolved once per call.

A test run can mint its own browser, so "the page" is ambiguous: the chat's tools drive one and the
run left another. This is the single resolver for that choice. There is deliberately no second
last-run lookup — a tool that resolved the run's session its own way could act in one browser and
report from the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from skyvern.forge.sdk.copilot.context import CopilotContext

BROWSER_TARGET_PARAM_NAME = "target"
BROWSER_TARGET_PARAM: dict[str, Any] = {
    "type": "string",
    "enum": ["debug", "last_run"],
    "description": (
        "Which browser to act in. 'debug' (default) is the scouting browser this chat drives. "
        "'last_run' is the browser the most recent test run executed in, which is a different "
        "browser whenever that run minted its own session — it is the only way to observe the page "
        "a run actually failed on. Acting rather than observing in 'last_run' changes that page: it "
        "can lose the state you are diagnosing, and any submit, purchase, or message it triggers is "
        "real. A run's recorded outcome is never changed by what you do afterwards."
    ),
}


@dataclass(frozen=True)
class BrowserSessionBinding:
    """One call's browser, resolved once and never read back off the shared context field.

    Sibling tool calls from the same model turn run concurrently against one context, so a binding
    that lived on ``ctx.browser_session_id`` could be read by the wrong call.
    """

    target: str
    # Set only when the model pointed this call at a browser other than the chat's own. The debug
    # target deliberately carries none, so a session re-established mid-dispatch still applies.
    session_id_override: str | None
    workflow_run_id: str | None
    source_matches_target: bool
    unavailable_reason: str | None = None

    def session_id_for(self, copilot_ctx: CopilotContext) -> str | None:
        return self.session_id_override or copilot_ctx.browser_session_id

    def provenance(self) -> dict[str, Any]:
        stamp: dict[str, Any] = {
            "browser_target": self.target,
            "source_matches_target": self.source_matches_target,
        }
        if self.workflow_run_id:
            # Deliberately not the bare "workflow_run_id": that key is read below into
            # ScreenshotProvenance, and a targeted read must not silently re-attribute a screenshot.
            stamp["browser_target_workflow_run_id"] = self.workflow_run_id
        if self.unavailable_reason:
            stamp["browser_target_unavailable"] = self.unavailable_reason
        return stamp


def resolve_browser_session_binding(copilot_ctx: CopilotContext, arguments: dict[str, Any]) -> BrowserSessionBinding:
    """Bind this call to the browser the model named, or report why that browser is not addressable.

    ``last_run`` is a promise about identity, so it is kept only against the exact recorded run
    session; an unknown one is reported as unavailable rather than silently served from the debug
    browser.
    """
    requested = arguments.get(BROWSER_TARGET_PARAM_NAME)
    if requested is not None and requested not in ("debug", "last_run"):
        return BrowserSessionBinding(
            target="debug",
            session_id_override=None,
            workflow_run_id=None,
            source_matches_target=False,
            unavailable_reason=f"Unknown browser target {requested!r}. Name 'debug' or 'last_run'.",
        )
    target = requested or "debug"
    if target == "debug":
        return BrowserSessionBinding(
            target="debug",
            session_id_override=None,
            workflow_run_id=None,
            source_matches_target=True,
        )
    run_session_id = copilot_ctx.last_run_blocks_browser_session_id
    workflow_run_id = copilot_ctx.last_run_blocks_workflow_run_id
    if not run_session_id:
        return BrowserSessionBinding(
            target="last_run",
            session_id_override=None,
            workflow_run_id=workflow_run_id,
            source_matches_target=False,
            unavailable_reason="No test run has recorded a browser session in this chat yet.",
        )
    if run_session_id == copilot_ctx.browser_session_id:
        # The run executed in this chat's own browser, so there is nothing to redirect. Overriding
        # would pin the call to a recorded id that a later re-establishment leaves behind, so the
        # flag records that the two matched when bound; a re-establishment between here and
        # dispatch moves the call to the live successor, and the session that acted is recoverable
        # from completion_browser_session_id and the continuity generation.
        return BrowserSessionBinding(
            target="last_run",
            session_id_override=None,
            workflow_run_id=workflow_run_id,
            source_matches_target=True,
        )
    return BrowserSessionBinding(
        target="last_run",
        session_id_override=run_session_id,
        workflow_run_id=workflow_run_id,
        source_matches_target=True,
    )
