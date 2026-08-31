"""Per-run decision for Task V3 auto-observe. The engine consumes it through the AgentFunction seam,
so an OSS deployment resolves from the static setting while cloud can bucket a run into an A/B."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from skyvern.config import settings

AutoObserveArm = Literal["treatment", "control", "setting", "default"]


@dataclass(frozen=True)
class AutoObserveDecision:
    enabled: bool
    arm: AutoObserveArm


def auto_observe_from_setting() -> AutoObserveDecision:
    if settings.TASK_V3_AUTO_OBSERVE:
        return AutoObserveDecision(enabled=True, arm="setting")
    return AutoObserveDecision(enabled=False, arm="default")
