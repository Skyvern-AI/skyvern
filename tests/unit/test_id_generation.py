import pytest

from skyvern.forge.sdk.db import id as id_module


def test_generate_id_uniqueness_with_overflow(monkeypatch: pytest.MonkeyPatch) -> None:
    total_ids = 10000
    generated_ids = [id_module.generate_id() for _ in range(total_ids)]
    assert len(set(generated_ids)) == total_ids
