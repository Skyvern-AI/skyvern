"""Tests for the bind_copilot_session_id context manager."""

from __future__ import annotations

import pytest

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.routes.workflow_copilot import bind_copilot_session_id


class TestBindCopilotSessionId:
    def test_sets_id_during_scope_when_ambient_context_present(self) -> None:
        with skyvern_context.scoped(SkyvernContext(copilot_session_id=None)):
            with bind_copilot_session_id("chat_xyz"):
                ctx = skyvern_context.current()
                assert ctx is not None
                assert ctx.copilot_session_id == "chat_xyz"

    def test_restores_prior_value_on_normal_exit(self) -> None:
        with skyvern_context.scoped(SkyvernContext(copilot_session_id="outer")):
            with bind_copilot_session_id("inner"):
                assert skyvern_context.current().copilot_session_id == "inner"  # type: ignore[union-attr]
            assert skyvern_context.current().copilot_session_id == "outer"  # type: ignore[union-attr]

    def test_restores_prior_value_when_body_raises(self) -> None:
        class _Boom(RuntimeError):
            pass

        with skyvern_context.scoped(SkyvernContext(copilot_session_id="outer")):
            with pytest.raises(_Boom):
                with bind_copilot_session_id("inner"):
                    raise _Boom("body raised")
            assert skyvern_context.current().copilot_session_id == "outer"  # type: ignore[union-attr]

    def test_noop_when_chat_id_is_none(self) -> None:
        with skyvern_context.scoped(SkyvernContext(copilot_session_id="outer")):
            with bind_copilot_session_id(None):
                # No overwrite — the outer value must stick.
                assert skyvern_context.current().copilot_session_id == "outer"  # type: ignore[union-attr]
            assert skyvern_context.current().copilot_session_id == "outer"  # type: ignore[union-attr]

    def test_noop_when_no_ambient_context(self) -> None:
        skyvern_context.reset()
        # Helper must not raise when there is no context to mutate — the
        # copilot route should still function, just without the tag.
        with bind_copilot_session_id("chat_xyz"):
            assert skyvern_context.current() is None
        assert skyvern_context.current() is None
