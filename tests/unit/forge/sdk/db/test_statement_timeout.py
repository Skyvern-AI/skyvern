import pytest
from sqlalchemy.exc import OperationalError

from skyvern.forge.sdk.db.statement_timeout import apply_statement_timeout, is_statement_timeout
from tests.unit.statement_timeout_fakes import statement_timeout_error


def test_is_statement_timeout_true_for_pg_cancellation() -> None:
    assert is_statement_timeout(statement_timeout_error()) is True


def test_is_statement_timeout_false_for_other_errors() -> None:
    # A real outage must stay distinguishable: callers translate a cancellation into a client-facing
    # response, so misreading one of these would mask a server fault.
    assert is_statement_timeout(ValueError("boom")) is False
    assert is_statement_timeout(OperationalError("x", {}, Exception("other"))) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (30, "30s"),
        # A caller splitting one budget across several statements passes the sub-second remainder.
        (0.25, "250ms"),
        (1.5, "1500ms"),
    ],
)
async def test_apply_statement_timeout_preserves_subsecond_precision(seconds: float, expected: str) -> None:
    executed: list[dict[str, str]] = []

    class _Session:
        async def execute(self, _statement: object, parameters: dict[str, str]) -> None:
            executed.append(parameters)

    await apply_statement_timeout(_Session(), seconds)  # type: ignore[arg-type]

    assert executed == [{"statement_timeout": expected}]


@pytest.mark.asyncio
async def test_apply_statement_timeout_rejects_a_spent_budget() -> None:
    # Postgres reads 0 as "no timeout", so a caller that let its budget reach zero must not silently
    # get an unbounded statement.
    class _Session:
        async def execute(self, _statement: object, _parameters: dict[str, str]) -> None:
            raise AssertionError("must not issue a statement for a non-positive budget")

    with pytest.raises(ValueError):
        await apply_statement_timeout(_Session(), 0)  # type: ignore[arg-type]
