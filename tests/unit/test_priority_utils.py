from unittest.mock import patch

from workers.temporal_v2_worker.priority_utils import (
    ComputeProfile,
    WorkloadType,
    get_temporal_v2_task_queue,
)

# Sample org IDs for testing (from cloud/config.py defaults)
NAT_ORG_ID = "o_385835488455492960"
BACKUP_ORG_ID = "o_364665147124050758"
REGULAR_ORG_ID = "o_999999999999999999"


class TestGetTemporalV2TaskQueue:
    """Test get_temporal_v2_task_queue prefix logic."""

    def test_regular_org_default(self) -> None:
        result = get_temporal_v2_task_queue(True, REGULAR_ORG_ID, WorkloadType.TASK)
        assert result == "patchright-1vcpu-4gb"

    def test_regular_org_workflow(self) -> None:
        result = get_temporal_v2_task_queue(True, REGULAR_ORG_ID, WorkloadType.WORKFLOW)
        assert result == "patchright-2vcpu-8gb"

    def test_regular_org_playwright(self) -> None:
        result = get_temporal_v2_task_queue(False, REGULAR_ORG_ID, WorkloadType.TASK)
        assert result == "playwright-1vcpu-4gb"

    def test_backup_org_default(self) -> None:
        result = get_temporal_v2_task_queue(True, BACKUP_ORG_ID, WorkloadType.TASK)
        assert result == "backup-patchright-1vcpu-4gb"

    def test_nat_org_default(self) -> None:
        result = get_temporal_v2_task_queue(True, NAT_ORG_ID, WorkloadType.TASK)
        assert result == "nat-patchright-1vcpu-4gb"

    def test_explicit_compute_profile(self) -> None:
        result = get_temporal_v2_task_queue(True, REGULAR_ORG_ID, WorkloadType.TASK, ComputeProfile.VCPU_4_8GB)
        assert result == "patchright-4vcpu-8gb"

    # --- Hetzner routing tests ---

    def test_regular_org_hetzner(self) -> None:
        result = get_temporal_v2_task_queue(True, REGULAR_ORG_ID, WorkloadType.TASK, use_hetzner=True)
        assert result == "hetzner-patchright-1vcpu-4gb"

    def test_regular_org_hetzner_workflow(self) -> None:
        result = get_temporal_v2_task_queue(True, REGULAR_ORG_ID, WorkloadType.WORKFLOW, use_hetzner=True)
        assert result == "hetzner-patchright-2vcpu-8gb"

    def test_regular_org_hetzner_playwright(self) -> None:
        result = get_temporal_v2_task_queue(False, REGULAR_ORG_ID, WorkloadType.TASK, use_hetzner=True)
        assert result == "hetzner-playwright-1vcpu-4gb"

    def test_backup_org_hetzner(self) -> None:
        result = get_temporal_v2_task_queue(True, BACKUP_ORG_ID, WorkloadType.TASK, use_hetzner=True)
        assert result == "hetzner-backup-patchright-1vcpu-4gb"

    def test_nat_org_hetzner_stays_on_nat(self) -> None:
        """NAT orgs should always use the nat- prefix, even with use_hetzner=True."""
        result = get_temporal_v2_task_queue(True, NAT_ORG_ID, WorkloadType.TASK, use_hetzner=True)
        assert result == "nat-patchright-1vcpu-4gb"

    def test_hetzner_with_explicit_compute_profile(self) -> None:
        result = get_temporal_v2_task_queue(
            True, REGULAR_ORG_ID, WorkloadType.TASK, ComputeProfile.VCPU_2_8GB, use_hetzner=True
        )
        assert result == "hetzner-patchright-2vcpu-8gb"

    def test_no_org_hetzner(self) -> None:
        result = get_temporal_v2_task_queue(True, None, WorkloadType.TASK, use_hetzner=True)
        assert result == "hetzner-patchright-1vcpu-4gb"

    def test_no_org_no_hetzner(self) -> None:
        result = get_temporal_v2_task_queue(True, None, WorkloadType.TASK, use_hetzner=False)
        assert result == "patchright-1vcpu-4gb"

    @patch("workers.temporal_v2_worker.priority_utils.settings")
    def test_respects_prefix_setting_when_not_hetzner(self, mock_settings: object) -> None:
        """When use_hetzner is False, the default prefix setting is used."""
        mock_settings.TEMPORAL_V2_TASK_QUEUE_PREFIX = "custom-"  # type: ignore[attr-defined]
        mock_settings.NAT_QUEUE_ORGANIZATION_IDS = []  # type: ignore[attr-defined]
        mock_settings.BACKUP_QUEUE_ORGANIZATION_IDS = []  # type: ignore[attr-defined]
        result = get_temporal_v2_task_queue(True, REGULAR_ORG_ID, WorkloadType.TASK, use_hetzner=False)
        assert result == "custom-patchright-1vcpu-4gb"
