"""Enrollment persistence and run binding for the browser action policy (SKY-12873)."""

from __future__ import annotations

import inspect
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from skyvern.exceptions import BrowserActionPolicyNotEnforceable
from skyvern.forge import app
from skyvern.forge.sdk.browser_action_policy import BrowserActionPolicy, declare_origin, declare_policy
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.workflow.browser_action_policy_enrollment import (
    POLICY_KEY,
    EnrollmentRejection,
    bind_policy_to_context,
    carried_policy,
    deserialize_policy,
    read_policy,
    rejection_reasons,
    serialize_policy,
    stored_policy_version,
    with_policy,
)
from skyvern.forge.sdk.workflow.models.workflow import Workflow
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.schemas.workflows import WorkflowCreateYAMLRequest


def _output_parameter(key: str) -> dict[str, Any]:
    return {
        "parameter_type": "output",
        "key": key,
        "output_parameter_id": f"op_{key}",
        "workflow_id": "wf_test",
        "created_at": datetime.now(timezone.utc),
        "modified_at": datetime.now(timezone.utc),
    }


def _navigation_block(label: str = "open_site") -> dict[str, Any]:
    return {
        "block_type": "navigation",
        "label": label,
        "url": "https://example.com/login",
        "navigation_goal": "Open the site",
        "output_parameter": _output_parameter(f"{label}_output"),
    }


def _code_block(label: str = "run_code") -> dict[str, Any]:
    return {
        "block_type": "code",
        "label": label,
        "code": "result = 1",
        "output_parameter": _output_parameter(f"{label}_output"),
    }


def _for_loop_block(inner: list[dict[str, Any]], label: str = "loop") -> dict[str, Any]:
    return {
        "block_type": "for_loop",
        "label": label,
        "loop_over_parameter_key": "items",
        "loop_blocks": inner,
        "output_parameter": _output_parameter(f"{label}_output"),
    }


def _workflow(blocks: list[dict[str, Any]] | None = None, **overrides: Any) -> Workflow:
    fields: dict[str, Any] = {
        "workflow_id": "wf_test",
        "organization_id": "o_test",
        "workflow_permanent_id": "wpid_test",
        "title": "test",
        "version": 1,
        "is_saved_task": False,
        "workflow_definition": {"parameters": [], "blocks": blocks if blocks is not None else [_navigation_block()]},
        "created_at": datetime.now(timezone.utc),
        "modified_at": datetime.now(timezone.utc),
    }
    fields.update(overrides)
    return Workflow(**fields)


def _policy(*origin_urls: str, owner_id: str = "o_test", version: int = 1) -> BrowserActionPolicy:
    return declare_policy(owner_id=owner_id, origin_urls=list(origin_urls) or ["https://example.com"], version=version)


class TestSerialization:
    def test_round_trip_preserves_the_policy(self) -> None:
        policy = _policy("https://example.com", "https://a.example.com:8443", version=7)
        assert deserialize_policy(serialize_policy(policy)) == policy

    def test_origin_order_is_deterministic_regardless_of_frozenset_iteration(self) -> None:
        urls = ["https://z.example.com", "https://a.example.com", "https://m.example.com:8443"]
        first = serialize_policy(declare_policy(owner_id="o_test", origin_urls=urls))
        second = serialize_policy(declare_policy(owner_id="o_test", origin_urls=list(reversed(urls))))
        assert first == second
        assert first["allowed_origins"] == sorted(first["allowed_origins"])

    @pytest.mark.parametrize("spelling", ["HTTPS://Example.COM:443/path", "https://example.com."])
    def test_serialized_origins_are_canonical(self, spelling: str) -> None:
        stored = serialize_policy(declare_policy(owner_id="o_test", origin_urls=[spelling]))
        assert stored["allowed_origins"] == [declare_origin(spelling).canonical]
        assert stored["allowed_origins"] == [declare_origin("https://example.com").canonical]

    @pytest.mark.parametrize(
        "corrupt",
        [
            None,
            "https://example.com",
            [],
            {"owner_id": "o_test", "allowed_origins": ["https://example.com"]},
            {"owner_id": "o_test", "allowed_origins": ["https://example.com"], "version": 1, "extra": 1},
            {"owner_id": "", "allowed_origins": ["https://example.com"], "version": 1},
            {"owner_id": "o_test", "allowed_origins": [], "version": 1},
            {"owner_id": "o_test", "allowed_origins": ["not a url"], "version": 1},
            {"owner_id": "o_test", "allowed_origins": ["file:///etc/passwd"], "version": 1},
            {"owner_id": "o_test", "allowed_origins": "https://example.com", "version": 1},
            {"owner_id": "o_test", "allowed_origins": [1], "version": 1},
            {"owner_id": 5, "allowed_origins": ["https://example.com"], "version": 1},
            {"owner_id": "o_test", "allowed_origins": ["https://example.com"], "version": "1"},
            {"owner_id": "o_test", "allowed_origins": ["https://example.com"], "version": True},
            # Hosts the core refuses because their spelling cannot be reconciled with what a browser
            # would resolve: non-ASCII (stdlib IDNA2003 diverges from browser UTS-46), non-ASCII
            # label separators, and last labels that read as address literals.
            {"owner_id": "o_test", "allowed_origins": ["https://exämple.com"], "version": 1},
            {"owner_id": "o_test", "allowed_origins": ["https://example。com。"], "version": 1},
            {"owner_id": "o_test", "allowed_origins": ["https://example.0x1"], "version": 1},
            {"owner_id": "o_test", "allowed_origins": ["https://example.12"], "version": 1},
        ],
    )
    def test_corrupt_policy_data_is_rejected(self, corrupt: object) -> None:
        with pytest.raises(ValueError):
            deserialize_policy(corrupt)

    def test_a_host_the_core_refuses_cannot_be_smuggled_in_through_storage(self) -> None:
        # Rehydration goes through declare_policy, so a stored origin the declaration rules would
        # never have minted stays unusable rather than becoming an allowlist entry on reload.
        with pytest.raises(ValueError):
            declare_policy(owner_id="o_test", origin_urls=["https://exämple.com"])
        with pytest.raises(ValueError):
            read_policy({POLICY_KEY: {"owner_id": "o_test", "allowed_origins": ["https://exämple.com"], "version": 1}})

    def test_an_internationalized_origin_is_declared_and_stored_as_its_a_label(self) -> None:
        # The core refuses the unicode spelling rather than encoding it, so an operator declares the
        # punycode form — which is what the browser puts on the wire anyway.
        stored = serialize_policy(declare_policy(owner_id="o_test", origin_urls=["https://xn--exmple-cua.com"]))
        assert stored["allowed_origins"] == [declare_origin("https://xn--exmple-cua.com").canonical]
        assert deserialize_policy(stored).allowed_origins == frozenset({declare_origin("https://xn--exmple-cua.com")})

    def test_read_policy_returns_none_for_an_unenrolled_definition(self) -> None:
        assert read_policy({"parameters": [], "blocks": []}) is None
        assert read_policy(None) is None

    def test_read_policy_raises_rather_than_silently_unenrolling_a_corrupt_row(self) -> None:
        with pytest.raises(ValueError):
            read_policy({"parameters": [], "blocks": [], POLICY_KEY: {"owner_id": "o_test"}})

    def test_a_yaml_save_cannot_express_a_policy_at_all(self) -> None:
        # The first line of defence: the request model has no such field, so a submitted policy is
        # dropped before any write path sees it — and no response model can echo one back out.
        stored = serialize_policy(_policy("https://evil.example.com"))
        request = WorkflowCreateYAMLRequest.model_validate(
            {
                "title": "smuggled",
                "workflow_definition": {"parameters": [], "blocks": [], POLICY_KEY: stored},
                POLICY_KEY: stored,
            }
        )
        assert POLICY_KEY not in request.model_dump(mode="json")
        assert POLICY_KEY not in request.workflow_definition.model_dump(mode="json")

    def test_with_policy_sets_and_clears_without_touching_the_definition(self) -> None:
        definition = {"parameters": [], "blocks": [_navigation_block()]}
        stored = serialize_policy(_policy())

        enrolled = with_policy(definition, stored)
        assert enrolled[POLICY_KEY] == stored
        assert enrolled["blocks"] == definition["blocks"]
        assert POLICY_KEY not in definition

        assert POLICY_KEY not in with_policy(enrolled, None)

    def test_carried_policy_copies_the_stored_value_verbatim(self) -> None:
        stored = {"owner_id": "o_test", "allowed_origins": ["https://example.com"], "version": 3}
        assert carried_policy({"blocks": [], POLICY_KEY: stored}) == stored
        assert carried_policy({"blocks": []}) is None
        assert carried_policy(None) is None

    def test_carried_policy_copies_corrupt_data_unchanged(self) -> None:
        # A save must not be able to alter policy, including "repairing" it into something valid.
        corrupt = {"owner_id": "o_test"}
        assert carried_policy({POLICY_KEY: corrupt}) == corrupt

    @pytest.mark.parametrize(
        "definition,expected",
        [
            ({}, 0),
            (None, 0),
            ({POLICY_KEY: {"version": 4}}, 4),
            ({POLICY_KEY: {"version": 0}}, 0),
            ({POLICY_KEY: {"version": True}}, 0),
            ({POLICY_KEY: {"version": "4"}}, 0),
            ({POLICY_KEY: "garbage"}, 0),
        ],
    )
    def test_stored_policy_version_never_raises(self, definition: object, expected: int) -> None:
        assert stored_policy_version(definition) == expected


class TestEligibility:
    def test_an_ordinary_agent_workflow_is_eligible(self) -> None:
        assert rejection_reasons(_workflow(), run_with=None) == ()

    def test_a_code_block_is_rejected(self) -> None:
        assert rejection_reasons(_workflow([_code_block()]), run_with=None) == (EnrollmentRejection.CODE_BLOCK_PRESENT,)

    def test_a_code_block_nested_in_a_loop_is_rejected(self) -> None:
        blocks = [_for_loop_block([_navigation_block("inner_nav"), _code_block("inner_code")])]
        assert EnrollmentRejection.CODE_BLOCK_PRESENT in rejection_reasons(_workflow(blocks), run_with=None)

    def test_workflow_level_code_execution_is_rejected(self) -> None:
        assert rejection_reasons(_workflow(run_with="code"), run_with=None) == (
            EnrollmentRejection.CODE_EXECUTION_SELECTED,
        )

    def test_run_level_code_execution_overrides_an_agent_workflow(self) -> None:
        assert rejection_reasons(_workflow(run_with="agent"), run_with="code") == (
            EnrollmentRejection.CODE_EXECUTION_SELECTED,
        )

    def test_run_level_agent_does_not_rescue_a_code_workflow(self) -> None:
        # The version still selects code execution; a single agent run does not make it enforceable.
        assert rejection_reasons(_workflow(run_with="code"), run_with="agent") == (
            EnrollmentRejection.CODE_EXECUTION_SELECTED,
        )

    def test_cached_and_generated_script_selection_is_rejected(self) -> None:
        assert rejection_reasons(_workflow(adaptive_caching=True), run_with=None) == (
            EnrollmentRejection.ADAPTIVE_CACHING_ENABLED,
        )
        assert rejection_reasons(_workflow(generate_script_on_terminal=True), run_with=None) == (
            EnrollmentRejection.SCRIPT_GENERATION_ENABLED,
        )

    def test_every_reason_is_reported_in_a_stable_order(self) -> None:
        workflow = _workflow([_code_block()], run_with="code", adaptive_caching=True, generate_script_on_terminal=True)
        assert rejection_reasons(workflow, run_with=None) == (
            EnrollmentRejection.CODE_BLOCK_PRESENT,
            EnrollmentRejection.CODE_EXECUTION_SELECTED,
            EnrollmentRejection.ADAPTIVE_CACHING_ENABLED,
            EnrollmentRejection.SCRIPT_GENERATION_ENABLED,
        )

    def test_reasons_carry_no_workflow_content(self) -> None:
        workflow = _workflow([_code_block("secret_label")], run_with="code")
        rendered = " ".join(rejection_reasons(workflow, run_with=None))
        assert "secret_label" not in rendered
        assert "example.com" not in rendered


class TestBinding:
    @pytest.fixture(autouse=True)
    def _fresh_context(self) -> Any:
        skyvern_context.set(SkyvernContext(organization_id="o_test"))
        yield
        skyvern_context.reset()

    def test_an_unenrolled_version_leaves_the_context_unenrolled(self) -> None:
        bind_policy_to_context(None, _workflow(), run_with=None)
        assert skyvern_context.ensure_context().browser_action_policy is None

    def test_an_enrolled_version_binds_the_exact_policy(self) -> None:
        policy = _policy("https://example.com")
        bind_policy_to_context(policy, _workflow(), run_with=None)
        assert skyvern_context.ensure_context().browser_action_policy is policy

    def test_an_unenrolled_version_clears_a_policy_left_on_a_reused_context(self) -> None:
        # A grant must not survive into a run whose own version is unenrolled.
        skyvern_context.ensure_context().browser_action_policy = _policy("https://example.com")
        bind_policy_to_context(None, _workflow(), run_with=None)
        assert skyvern_context.ensure_context().browser_action_policy is None

    def test_an_ineligible_enrolled_version_is_rejected_and_binds_nothing(self) -> None:
        with pytest.raises(BrowserActionPolicyNotEnforceable) as excinfo:
            bind_policy_to_context(_policy(), _workflow([_code_block()]), run_with=None)
        assert excinfo.value.reasons == (EnrollmentRejection.CODE_BLOCK_PRESENT,)
        assert skyvern_context.ensure_context().browser_action_policy is None

    def test_rejection_message_carries_only_stable_reason_codes(self) -> None:
        with pytest.raises(BrowserActionPolicyNotEnforceable) as excinfo:
            bind_policy_to_context(_policy("https://example.com"), _workflow(run_with="code"), run_with=None)
        message = str(excinfo.value.message)
        assert EnrollmentRejection.CODE_EXECUTION_SELECTED in message
        assert "example.com" not in message
        assert "wpid_test" not in message

    def test_an_ineligible_unenrolled_version_is_left_alone(self) -> None:
        # Code blocks and script execution are only refused for versions an operator enrolled.
        bind_policy_to_context(None, _workflow([_code_block()], run_with="code"), run_with="code")
        assert skyvern_context.ensure_context().browser_action_policy is None

    def test_binding_an_enrolled_policy_without_a_context_fails_closed(self) -> None:
        # Nothing could consume the policy, so the run must not proceed as if it had been bound.
        skyvern_context.reset()
        with pytest.raises(RuntimeError):
            bind_policy_to_context(_policy(), _workflow(), run_with=None)

    def test_binding_an_unenrolled_version_without_a_context_is_a_no_op(self) -> None:
        skyvern_context.reset()
        bind_policy_to_context(None, _workflow(), run_with=None)
        assert skyvern_context.current() is None


class TestExecutionPathBinding:
    @pytest.fixture(autouse=True)
    def _fresh_context(self) -> Any:
        skyvern_context.set(SkyvernContext(organization_id="o_test"))
        yield
        skyvern_context.reset()

    @pytest.mark.asyncio
    async def test_the_service_binds_the_resolved_workflow_versions_policy(self) -> None:
        policy = _policy("https://example.com")
        app.DATABASE.workflows.get_browser_action_policy = AsyncMock(return_value=policy)
        workflow = _workflow()

        await WorkflowService().bind_browser_action_policy(workflow, run_with=None)

        app.DATABASE.workflows.get_browser_action_policy.assert_awaited_once_with(
            workflow_id=workflow.workflow_id, organization_id=workflow.organization_id
        )
        assert skyvern_context.ensure_context().browser_action_policy is policy

    @pytest.mark.asyncio
    async def test_the_service_rejects_an_ineligible_enrolled_version(self) -> None:
        app.DATABASE.workflows.get_browser_action_policy = AsyncMock(return_value=_policy("https://example.com"))
        with pytest.raises(BrowserActionPolicyNotEnforceable):
            await WorkflowService().bind_browser_action_policy(_workflow([_code_block()]), run_with=None)

    @pytest.mark.asyncio
    async def test_a_child_workflow_binds_its_own_policy_over_the_parents(self) -> None:
        parent_policy = _policy("https://parent.example.com")
        child_policy = _policy("https://child.example.com")
        service = WorkflowService()

        app.DATABASE.workflows.get_browser_action_policy = AsyncMock(return_value=parent_policy)
        await service.bind_browser_action_policy(_workflow(), run_with=None)

        # A child workflow run replaces the context; binding must not inherit the parent's grant.
        skyvern_context.replace(SkyvernContext(organization_id="o_test"))
        app.DATABASE.workflows.get_browser_action_policy = AsyncMock(return_value=child_policy)
        await service.bind_browser_action_policy(_workflow(workflow_id="wf_child"), run_with=None)

        assert skyvern_context.ensure_context().browser_action_policy is child_policy

    @pytest.mark.asyncio
    async def test_an_unenrolled_child_does_not_inherit_the_parents_grant(self) -> None:
        service = WorkflowService()
        app.DATABASE.workflows.get_browser_action_policy = AsyncMock(return_value=_policy("https://parent.example.com"))
        await service.bind_browser_action_policy(_workflow(), run_with=None)

        app.DATABASE.workflows.get_browser_action_policy = AsyncMock(return_value=None)
        await service.bind_browser_action_policy(_workflow(workflow_id="wf_child"), run_with=None)

        assert skyvern_context.ensure_context().browser_action_policy is None

    @pytest.mark.parametrize("path", ["setup_workflow_run", "execute_workflow"])
    def test_execution_paths_bind_before_any_browser_is_created(self, path: str) -> None:
        source = inspect.getsource(getattr(WorkflowService, path))
        bind_at = source.find("bind_browser_action_policy")
        assert bind_at != -1, f"{path} must bind the workflow-version policy"
        for browser_call in ("BROWSER_MANAGER", "auto_create_browser_session"):
            first_use = source.find(browser_call)
            assert first_use == -1 or bind_at < first_use, f"{path} creates a browser before binding policy"

    @pytest.mark.parametrize("path", ["setup_workflow_run", "execute_workflow"])
    def test_a_rejected_binding_fails_the_run_rather_than_stranding_it(self, path: str) -> None:
        # The workflow_run row exists by the time either site binds, so neither may let the
        # rejection escape without moving the run out of its non-final state.
        source = inspect.getsource(getattr(WorkflowService, path))
        assert "BrowserActionPolicyNotEnforceable" in source
        assert "mark_workflow_run_as_failed" in source

    @pytest.mark.asyncio
    async def test_an_enrolled_run_never_selects_cached_script_execution(self) -> None:
        # An action-level firewall cannot see inside generated script execution, so the rollout that
        # upgrades ordinary runs to code mode must not reach an enrolled run.
        skyvern_context.ensure_context().browser_action_policy = _policy("https://example.com")
        app.AGENT_FUNCTION.should_upgrade_to_code_mode = AsyncMock(return_value=True)
        app.AGENT_FUNCTION.should_keep_code_mode_for_workflow_run = AsyncMock(return_value=True)

        workflow_run = SimpleNamespace(run_with=None, retried_from_workflow_run_id=None)
        decision = await WorkflowService().should_run_script(_workflow(), cast(Any, workflow_run))

        assert decision is False
        app.AGENT_FUNCTION.should_upgrade_to_code_mode.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unenrolled_run_still_reaches_the_code_mode_rollout(self) -> None:
        app.AGENT_FUNCTION.should_upgrade_to_code_mode = AsyncMock(return_value=True)
        app.AGENT_FUNCTION.should_keep_code_mode_for_workflow_run = AsyncMock(return_value=True)

        workflow_run = SimpleNamespace(run_with=None, retried_from_workflow_run_id=None)
        assert await WorkflowService().should_run_script(_workflow(), cast(Any, workflow_run)) is True


class TestNoUnguardedDefinitionWriter:
    """`with_policy` is only a guarantee if it is the ONLY thing that writes the definition column.

    Proving that by construction is what lets an erasure slip in later: any writer that reads the
    definition into `WorkflowDefinition` and writes the model back drops the reserved key and
    silently unenrolls the workflow. These pin the writer set instead of arguing about it.
    """

    ROOTS = ("skyvern", "cloud", "scripts", "alembic")
    # Writes a mutated deepcopy of the raw dict and never rebuilds it, so unknown top-level keys
    # survive. Pinned behaviourally by test_the_secret_backfill_transform_preserves_the_policy.
    ALLOWLIST = {"scripts/backfill_encrypt_file_block_secrets.py"}

    def _write_sites(self) -> list[tuple[str, int, str]]:
        repo_root = Path(__file__).resolve().parents[2]
        pattern = re.compile(r"\.workflow_definition\s*=[^=]")
        sites: list[tuple[str, int, str]] = []
        for root in self.ROOTS:
            for path in (repo_root / root).rglob("*.py"):
                relative = path.relative_to(repo_root).as_posix()
                for number, line in enumerate(path.read_text().splitlines(), start=1):
                    if pattern.search(line):
                        sites.append((relative, number, line.strip()))
        return sites

    def test_the_search_actually_finds_the_known_writers(self) -> None:
        # Without this the guard below passes vacuously if the pattern ever stops matching.
        files = {relative for relative, _, _ in self._write_sites()}
        assert "skyvern/forge/sdk/db/repositories/workflows.py" in files
        assert self.ALLOWLIST <= files

    def test_every_definition_write_goes_through_with_policy(self) -> None:
        unguarded = [
            (relative, number, line)
            for relative, number, line in self._write_sites()
            if relative not in self.ALLOWLIST and "with_policy" not in line
        ]
        assert unguarded == [], (
            "These write the workflow_definition column without carrying the stored browser action "
            "policy forward, which silently unenrolls the workflow: " + repr(unguarded)
        )
