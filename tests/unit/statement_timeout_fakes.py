from sqlalchemy.exc import OperationalError


class FakeQueryCanceled(Exception):
    """A psycopg query_canceled, which is what a ``statement_timeout`` expiry surfaces as."""

    sqlstate = "57014"


def statement_timeout_error(statement: str = "SELECT 1") -> OperationalError:
    """A SQLAlchemy error shaped like the one a cancelled statement raises."""
    return OperationalError(statement, {}, FakeQueryCanceled("canceling statement due to statement timeout"))
