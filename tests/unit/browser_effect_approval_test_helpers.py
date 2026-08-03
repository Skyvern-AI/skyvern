"""Sanctioned unit-test access to a real, dispatch-scoped ConsumedEffect."""

from collections.abc import Awaitable, Callable
from typing import TypeVar

from skyvern.forge.sdk.browser_action_policy import ProtectedReference
from skyvern.forge.sdk.browser_effect_approval import (
    ApprovalMode,
    ConsumedEffect,
    EffectApprovalStore,
    FrozenEffectDescriptor,
    TrustedEffectDispatcher,
)

T = TypeVar("T")


class _NoReferencesResolver:
    async def resolve(self, ref: ProtectedReference, run_id: str, consumer_id: str) -> str:
        raise AssertionError("The test helper descriptor has no protected references")


async def run_with_consumed_effect(callback: Callable[[ConsumedEffect], Awaitable[T]]) -> T:
    """Run ``callback`` with a genuine capability that expires when the callback returns."""

    descriptor = FrozenEffectDescriptor(
        run_identity="test_run",
        action_nonce="test_nonce",
        sink_sequence=0,
        observation_epoch=0,
        page_identity="test_page",
        tab_identity="test_tab",
        frame_identity="test_frame",
        sink_kind="test.sink",
        canonical_target="test://effect",
        canonical_method="test",
    )

    async def dispatch(consumed: ConsumedEffect, _: FrozenEffectDescriptor, __: tuple[str, ...]) -> T:
        return await callback(consumed)

    dispatcher = TrustedEffectDispatcher("test.consumer", (), dispatch)
    store = EffectApprovalStore(mode=ApprovalMode.ENFORCE, resolver=_NoReferencesResolver())
    return await store.consume_and_dispatch(store.preview(descriptor, dispatcher), descriptor, dispatcher)
