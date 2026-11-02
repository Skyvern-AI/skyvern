from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# SQLSTATE 57014 is query_canceled, which a statement_timeout expiry raises.
_STATEMENT_TIMEOUT_SQLSTATE = "57014"


def is_statement_timeout(exc: BaseException) -> bool:
    orig: BaseException = exc.orig if isinstance(exc, DBAPIError) and exc.orig is not None else exc
    # sqlstate is a driver attribute, so it is absent on anything that is not a DBAPI error.
    return getattr(orig, "sqlstate", None) == _STATEMENT_TIMEOUT_SQLSTATE


async def apply_statement_timeout(session: AsyncSession, statement_timeout_seconds: float) -> None:
    """Set a transaction-local ``statement_timeout``; call as the first execute in a session.

    Transaction-local rather than per-session so it also holds behind a transaction pooler, which
    hands out connections whose session-level settings the caller does not own.

    PostgreSQL applies the timeout to each statement separately, so a caller running several
    statements per request has to re-apply its remaining budget before each one to bound the whole
    request. Keep millisecond precision for fractional-second remainders.
    """
    if statement_timeout_seconds <= 0:
        raise ValueError("statement_timeout_seconds must be positive")
    milliseconds = max(1, round(statement_timeout_seconds * 1000))
    timeout = f"{milliseconds // 1000}s" if milliseconds % 1000 == 0 else f"{milliseconds}ms"
    await session.execute(
        text("SELECT set_config('statement_timeout', :statement_timeout, true)"),
        {"statement_timeout": timeout},
    )
