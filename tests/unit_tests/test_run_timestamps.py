from datetime import datetime
import sys
from pathlib import Path
import types
import pytest

pytest.skip("Dependencies missing", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
ddtrace_stub = types.SimpleNamespace(tracer=None, filters=types.SimpleNamespace(FilterRequestsOnUrl=lambda x: None))
sys.modules.setdefault("ddtrace", ddtrace_stub)
sys.modules.setdefault("ddtrace.filters", ddtrace_stub.filters)

from importlib import import_module

Task = import_module("skyvern.forge.sdk.schemas.tasks").Task
TaskBase = import_module("skyvern.forge.sdk.schemas.tasks").TaskBase
TaskStatus = import_module("skyvern.forge.sdk.schemas.tasks").TaskStatus


def test_task_response_timestamps() -> None:
    base = TaskBase(title="t", url="https://example.com")
    now = datetime.utcnow()
    task = Task(
        **base.model_dump(),
        created_at=now,
        modified_at=now,
        task_id="tsk_1",
        status=TaskStatus.completed,
        queued_at=now,
        started_at=now,
        finished_at=now,
    )
    resp = task.to_task_response()
    assert resp.queued_at == now
    assert resp.started_at == now
    assert resp.finished_at == now
