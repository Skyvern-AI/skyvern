import subprocess
import sys
import textwrap


def test_single_copilot_runtime_imports_without_cloud_package() -> None:
    script = textwrap.dedent(
        """
        import importlib.abc
        import asyncio
        import json
        import sys
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        import pytest

        class BlockCloud(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path, target=None):
                if fullname == "cloud" or fullname.startswith("cloud."):
                    raise ImportError(f"cloud package is unavailable: {fullname}")
                return None

        sys.meta_path.insert(0, BlockCloud())
        from skyvern.forge import app, set_force_app_instance
        from skyvern.forge.agent_functions import AgentFunction
        from skyvern.forge.sdk.copilot.agent import run_copilot_agent
        from skyvern.forge.sdk.routes import workflow_copilot as route
        from skyvern.forge.sdk.schemas.workflow_copilot import (
            WorkflowCopilotChatRequest,
            WorkflowCopilotStreamResponseUpdate,
        )
        from tests.unit.copilot_route_test_support import install_fake_create, setup_new_copilot_mocks
        from tests.unit.copilot_test_helpers import stub_copilot_agent_loop
        from tests.unit.force_stub_app import create_forge_stub_app

        async def serve_turns():
            forge_app = create_forge_stub_app()
            forge_app.AGENT_FUNCTION = AgentFunction()
            set_force_app_instance(forge_app)
            assert type(app.AGENT_FUNCTION).__module__ == "skyvern.forge.agent_functions"
            monkeypatch = pytest.MonkeyPatch()
            captured = install_fake_create(monkeypatch)
            chat = SimpleNamespace(
                workflow_copilot_chat_id="chat-test",
                workflow_permanent_id="wpid-test",
                organization_id="org-test",
                proposed_workflow=None,
                auto_accept=False,
            )
            original_workflow = SimpleNamespace(
                workflow_id="wf-test",
                title="Test",
                description=None,
                workflow_definition=None,
            )
            setup_new_copilot_mocks(
                monkeypatch,
                chat,
                original_workflow,
                SimpleNamespace(user_response="unused", updated_workflow=None, global_llm_context=None),
            )
            app.AGENT_FUNCTION.resolve_org_api_key = AsyncMock(return_value="sk-test")
            safety_handler = AsyncMock(
                return_value={"version": "1", "state": "clean", "handling": "none", "citations": []}
            )
            monkeypatch.setattr(
                route, "resolve_raw_secret_safety_handler", AsyncMock(return_value=safety_handler)
            )
            served_turns = 0

            async def deterministic_agent_turn(**kwargs):
                nonlocal served_turns
                served_turns += 1
                return SimpleNamespace(
                    final_output=json.dumps(
                        {"type": "REPLY", "user_response": "served by the agent runtime"}
                    ),
                    new_items=[],
                )

            stub_copilot_agent_loop(monkeypatch, deterministic_agent_turn)
            monkeypatch.setattr(route, "run_copilot_agent", run_copilot_agent)
            request = SimpleNamespace(headers={})
            organization = SimpleNamespace(organization_id="org-test")
            for code_block in (False, True):
                chat_request = WorkflowCopilotChatRequest(
                    workflow_permanent_id="wpid-test",
                    workflow_id="wf-test",
                    workflow_copilot_chat_id="chat-test",
                    message="hello",
                    workflow_yaml="title: Test",
                    mode="build",
                    code_block=code_block,
                )
                response = await route.workflow_copilot_chat_post(request, chat_request, organization)
                assert response is captured["sentinel"]
                stream = MagicMock(send=AsyncMock(return_value=True))
                await captured["handler"](stream)
                frames = [call.args[0] for call in stream.send.await_args_list if call.args]
                terminal = [frame for frame in frames if isinstance(frame, WorkflowCopilotStreamResponseUpdate)]
                assert terminal
                assert terminal[-1].message == "served by the agent runtime"
                assert terminal[-1].updated_workflow is None
                assert terminal[-1].workflow_applied is False

            assert served_turns == 2
            assert "cloud" not in sys.modules
            monkeypatch.undo()

        asyncio.run(serve_turns())
        """
    )

    subprocess.run([sys.executable, "-c", script], check=True, timeout=30)
