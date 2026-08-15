"""A challenge in page evidence is an observation the model reads, never an instruction to stop.

The veto that refused a block-run tool on this evidence is gone. These pin the quieter form of the
same rule: no prompt, tool description, or tool result may condition "stop and report" on
`challenge_state`, because a page that looks challenged has not established that a run will fail —
in the production session this ticket came from, the belief was wrong three times while the solver
succeeded 42 times on the same site.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The surfaces the model actually reads: its standing prompt, the description on the tool that
# produces the evidence, and the run tool's own budget-exit result.
MODEL_FACING_SOURCES = (
    REPO_ROOT / "skyvern/forge/prompts/skyvern/workflow-copilot-agent.j2",
    REPO_ROOT / "skyvern/forge/sdk/copilot/tools/__init__.py",
    REPO_ROOT / "skyvern/forge/sdk/copilot/tools/run_execution.py",
)

STOP_PHRASES = (
    "stop and report the anti-bot blocker",
    "stop and report the observed anti-bot",
    "report the observed anti-bot blocker rather than retrying",
    "treat challenge resolution",
)


@pytest.mark.parametrize("source", MODEL_FACING_SOURCES, ids=lambda p: p.name)
def test_no_model_facing_surface_orders_a_stop_from_challenge_evidence(source: Path) -> None:
    text = source.read_text()
    found = [phrase for phrase in STOP_PHRASES if phrase in text]
    assert not found, f"{source.name} still tells the model to stop on challenge evidence: {found}"


@pytest.mark.parametrize("source", MODEL_FACING_SOURCES, ids=lambda p: p.name)
def test_challenge_state_is_never_the_condition_for_abandoning_a_run(source: Path) -> None:
    # Reading challenge_state is fine and expected; conditioning a retreat on it is not.
    for line_number, line in enumerate(source.read_text().splitlines(), start=1):
        if "gates_submit_controls" not in line:
            continue
        lowered = line.lower()
        assert "stop and report" not in lowered, f"{source.name}:{line_number} conditions a stop on challenge state"
        assert "rather than retrying" not in lowered, (
            f"{source.name}:{line_number} conditions a retreat on challenge state"
        )
