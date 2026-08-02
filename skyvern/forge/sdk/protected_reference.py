"""Consumer-bound resolution contract for browser-firewall protected references."""

from __future__ import annotations

from typing import Protocol

from skyvern.forge.sdk.browser_action_policy import ProtectedReference


class ProtectedReferenceResolver(Protocol):
    """Capability exposed only to final trusted sinks."""

    async def resolve(self, ref: ProtectedReference, run_id: str, consumer_id: str) -> str: ...
