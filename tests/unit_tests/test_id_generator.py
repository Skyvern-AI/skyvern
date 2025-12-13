import re

import pytest

from skyvern.forge.sdk.db.id import (
    ACTION_PREFIX,
    ARTIFACT_PREFIX,
    BASE_EPOCH,
    CREDENTIAL_PREFIX,
    DEBUG_SESSION_PREFIX,
    FOLDER_PREFIX,
    ORG_PREFIX,
    SEQUENCE_BITS,
    SEQUENCE_MAX,
    STEP_PREFIX,
    TASK_PREFIX,
    TASK_V2_ID,
    THOUGHT_ID,
    TIMESTAMP_BITS,
    TOTP_CODE_PREFIX,
    USER_PREFIX,
    VERSION,
    VERSION_BITS,
    WORKER_ID_BITS,
    WORKFLOW_PREFIX,
    WORKFLOW_RUN_BLOCK_PREFIX,
    WORKFLOW_RUN_PREFIX,
    _mask_shift,
    current_time,
    current_time_ms,
    generate_action_id,
    generate_artifact_id,
    generate_credential_id,
    generate_debug_session_id,
    generate_folder_id,
    generate_id,
    generate_org_id,
    generate_step_id,
    generate_task_id,
    generate_task_v2_id,
    generate_thought_id,
    generate_totp_code_id,
    generate_user_id,
    generate_workflow_id,
    generate_workflow_run_block_id,
    generate_workflow_run_id,
)


class TestConstants:
    """Tests for ID generator constants."""

    def test_base_epoch(self):
        """BASE_EPOCH should be June 20, 2022 12:00 AM UTC."""
        assert BASE_EPOCH == 1655683200

    def test_version(self):
        """Version should be 0."""
        assert VERSION == 0

    def test_bit_allocation(self):
        """Total bits should add up to 64."""
        total_bits = TIMESTAMP_BITS + WORKER_ID_BITS + SEQUENCE_BITS + VERSION_BITS
        assert total_bits == 64

    def test_sequence_max(self):
        """SEQUENCE_MAX should be 2^10 - 1 = 1023."""
        assert SEQUENCE_MAX == 1023


class TestMaskShift:
    """Tests for the _mask_shift helper function."""

    def test_mask_shift_basic(self):
        """Basic mask and shift operation."""
        # value=5 (binary: 101), mask=3 bits, shift=2
        # masked: 5 & 0b111 = 5
        # shifted: 5 << 2 = 20
        result = _mask_shift(5, 3, 2)
        assert result == 20

    def test_mask_shift_overflow(self):
        """Value exceeding mask bits should be truncated."""
        # value=15 (binary: 1111), mask=2 bits
        # masked: 15 & 0b11 = 3
        # shifted: 3 << 0 = 3
        result = _mask_shift(15, 2, 0)
        assert result == 3

    def test_mask_shift_zero(self):
        """Zero value should return zero."""
        assert _mask_shift(0, 10, 5) == 0

    def test_mask_shift_max_bits(self):
        """Test with maximum number of bits."""
        value = (1 << 32) - 1  # Max 32-bit value
        result = _mask_shift(value, 32, 0)
        assert result == value


class TestCurrentTime:
    """Tests for time-related functions."""

    def test_current_time_is_integer(self):
        """current_time should return an integer."""
        assert isinstance(current_time(), int)

    def test_current_time_is_reasonable(self):
        """current_time should be a reasonable Unix timestamp."""
        now = current_time()
        # Should be after BASE_EPOCH
        assert now > BASE_EPOCH
        # Should be within reasonable bounds (before year 2100)
        assert now < 4102444800

    def test_current_time_ms_is_integer(self):
        """current_time_ms should return an integer."""
        assert isinstance(current_time_ms(), int)

    def test_current_time_ms_is_milliseconds(self):
        """current_time_ms should be roughly 1000x current_time."""
        t = current_time()
        t_ms = current_time_ms()
        # Allow for timing differences
        assert abs(t_ms - t * 1000) < 2000


class TestGenerateId:
    """Tests for the generate_id function."""

    def test_generate_id_returns_integer(self):
        """generate_id should return an integer."""
        assert isinstance(generate_id(), int)

    def test_generate_id_is_positive(self):
        """generate_id should return a positive integer."""
        assert generate_id() > 0

    def test_generate_id_is_64_bit(self):
        """generate_id should fit in 64 bits."""
        id_val = generate_id()
        assert id_val < (1 << 64)

    def test_generate_id_uniqueness(self):
        """Multiple calls should generate unique IDs."""
        ids = [generate_id() for _ in range(1000)]
        assert len(set(ids)) == 1000

    def test_generate_id_monotonic(self):
        """IDs generated in sequence should generally be increasing."""
        id1 = generate_id()
        id2 = generate_id()
        id3 = generate_id()
        # Due to sequence counter, IDs should increase
        # (within the same second, at least)
        assert id1 < id2 < id3


class TestPrefixedIdGenerators:
    """Tests for all prefixed ID generator functions."""

    @pytest.mark.parametrize(
        "generator,prefix",
        [
            (generate_workflow_id, WORKFLOW_PREFIX),
            (generate_workflow_run_id, WORKFLOW_RUN_PREFIX),
            (generate_workflow_run_block_id, WORKFLOW_RUN_BLOCK_PREFIX),
            (generate_task_id, TASK_PREFIX),
            (generate_step_id, STEP_PREFIX),
            (generate_artifact_id, ARTIFACT_PREFIX),
            (generate_user_id, USER_PREFIX),
            (generate_org_id, ORG_PREFIX),
            (generate_action_id, ACTION_PREFIX),
            (generate_task_v2_id, TASK_V2_ID),
            (generate_thought_id, THOUGHT_ID),
            (generate_totp_code_id, TOTP_CODE_PREFIX),
            (generate_credential_id, CREDENTIAL_PREFIX),
            (generate_debug_session_id, DEBUG_SESSION_PREFIX),
            (generate_folder_id, FOLDER_PREFIX),
        ],
    )
    def test_id_has_correct_prefix(self, generator, prefix):
        """Generated ID should have the correct prefix."""
        generated_id = generator()
        assert generated_id.startswith(f"{prefix}_")

    @pytest.mark.parametrize(
        "generator",
        [
            generate_workflow_id,
            generate_workflow_run_id,
            generate_task_id,
            generate_step_id,
            generate_artifact_id,
            generate_user_id,
            generate_org_id,
            generate_action_id,
        ],
    )
    def test_id_format(self, generator):
        """Generated ID should match expected format: prefix_integer."""
        generated_id = generator()
        assert re.match(r"^[a-z_]+_\d+$", generated_id)

    @pytest.mark.parametrize(
        "generator",
        [
            generate_workflow_id,
            generate_task_id,
            generate_org_id,
        ],
    )
    def test_id_uniqueness(self, generator):
        """Multiple calls should generate unique IDs."""
        ids = [generator() for _ in range(100)]
        assert len(set(ids)) == 100

    def test_different_generators_produce_different_prefixes(self):
        """Different generators should produce IDs with different prefixes."""
        task_id = generate_task_id()
        step_id = generate_step_id()
        org_id = generate_org_id()

        assert task_id.split("_")[0] != step_id.split("_")[0]
        assert task_id.split("_")[0] != org_id.split("_")[0]
        assert step_id.split("_")[0] != org_id.split("_")[0]


class TestIdExtraction:
    """Tests for extracting components from generated IDs."""

    def test_extract_numeric_part(self):
        """Should be able to extract numeric part from ID."""
        task_id = generate_task_id()
        parts = task_id.split("_")
        numeric_part = int(parts[-1])
        assert numeric_part > 0

    def test_numeric_part_is_valid_64bit(self):
        """Numeric part should be a valid 64-bit integer."""
        task_id = generate_task_id()
        numeric_part = int(task_id.split("_")[-1])
        assert numeric_part < (1 << 64)
