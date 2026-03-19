import importlib
import sys
from types import ModuleType, SimpleNamespace

import pytest


def _install_temporal_activity_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    temporalio_package = ModuleType("temporalio")
    temporalio_package.__path__ = []
    temporalio_activity = ModuleType("temporalio.activity")
    temporalio_activity.defn = lambda fn: fn
    temporalio_package.activity = temporalio_activity

    monkeypatch.setitem(sys.modules, "temporalio", temporalio_package)
    monkeypatch.setitem(sys.modules, "temporalio.activity", temporalio_activity)


def import_cron_worker_activities(monkeypatch: pytest.MonkeyPatch):
    cloud_package = ModuleType("cloud")
    cloud_package.__path__ = []
    cloud_clients = ModuleType("cloud.clients")
    cloud_yescaptcha = ModuleType("cloud.clients.yescaptcha")
    cloud_yescaptcha_client = ModuleType("cloud.clients.yescaptcha.client")
    cloud_config = ModuleType("cloud.config")
    cloud_db = ModuleType("cloud.db")
    cloud_agent_db = ModuleType("cloud.db.cloud_agent_db")
    cloud_tasks = ModuleType("cloud.tasks")

    class YescaptchaClient:
        def __init__(self, client_key: str) -> None:
            self.client_key = client_key

    async def update_stuck_tasks_to_timed_out() -> None:
        return None

    async def update_stuck_workflow_runs_to_timed_out() -> None:
        return None

    cloud_yescaptcha_client.YescaptchaClient = YescaptchaClient
    cloud_config.settings = SimpleNamespace(
        ENABLE_YESCAPTCHA_BALANCE_ALERT=False,
        YESCAPTCHA_API_KEY="",
    )
    cloud_agent_db.cloud_db = SimpleNamespace()
    cloud_tasks.update_stuck_tasks_to_timed_out = update_stuck_tasks_to_timed_out
    cloud_tasks.update_stuck_workflow_runs_to_timed_out = update_stuck_workflow_runs_to_timed_out

    monkeypatch.setitem(sys.modules, "cloud", cloud_package)
    monkeypatch.setitem(sys.modules, "cloud.clients", cloud_clients)
    monkeypatch.setitem(sys.modules, "cloud.clients.yescaptcha", cloud_yescaptcha)
    monkeypatch.setitem(sys.modules, "cloud.clients.yescaptcha.client", cloud_yescaptcha_client)
    monkeypatch.setitem(sys.modules, "cloud.config", cloud_config)
    monkeypatch.setitem(sys.modules, "cloud.db", cloud_db)
    monkeypatch.setitem(sys.modules, "cloud.db.cloud_agent_db", cloud_agent_db)
    monkeypatch.setitem(sys.modules, "cloud.tasks", cloud_tasks)
    _install_temporal_activity_stubs(monkeypatch)

    sys.modules.pop("workers.cron_worker.activities", None)
    return importlib.import_module("workers.cron_worker.activities")


def import_temporal_v2_worker_activities(monkeypatch: pytest.MonkeyPatch):
    cloud_package = ModuleType("cloud")
    cloud_package.__path__ = []
    cloud_services = ModuleType("cloud.services")
    data_scrubber_module = ModuleType("cloud.services.data_scrubber_service")
    worker_utils_module = ModuleType("workers.worker_utils")

    class DataScrubber:
        pass

    async def activity_teardown() -> None:
        return None

    data_scrubber_module.DataScrubber = DataScrubber
    worker_utils_module.activity_teardown = activity_teardown

    monkeypatch.setitem(sys.modules, "cloud", cloud_package)
    monkeypatch.setitem(sys.modules, "cloud.services", cloud_services)
    monkeypatch.setitem(sys.modules, "cloud.services.data_scrubber_service", data_scrubber_module)
    monkeypatch.setitem(sys.modules, "workers.worker_utils", worker_utils_module)
    _install_temporal_activity_stubs(monkeypatch)

    sys.modules.pop("workers.temporal_v2_worker.activities", None)
    return importlib.import_module("workers.temporal_v2_worker.activities")
