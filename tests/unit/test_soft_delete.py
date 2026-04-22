"""Tests for SoftDeleteMixin and exclude_deleted() helper."""

from datetime import datetime

import pytest
from sqlalchemy import Column, String, create_engine, select
from sqlalchemy.orm import Session

from skyvern.forge.sdk.db._soft_delete import SoftDeleteMixin, exclude_deleted
from skyvern.forge.sdk.db.models import Base


# Test model that uses the mixin
class FakeModel(SoftDeleteMixin, Base):
    __tablename__ = "fake_soft_delete_test"
    id = Column(String, primary_key=True)


@pytest.fixture()
def db_session():
    """Create an in-memory SQLite database with the test model table."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[FakeModel.__table__])
    with Session(engine) as session:
        yield session


class TestSoftDeleteMixin:
    def test_deleted_at_defaults_to_none(self, db_session: Session) -> None:
        """New records should have deleted_at=None by default."""
        record = FakeModel(id="1")
        db_session.add(record)
        db_session.commit()
        db_session.refresh(record)
        assert record.deleted_at is None

    def test_mark_deleted_sets_timestamp(self, db_session: Session) -> None:
        """mark_deleted() should set deleted_at to a datetime."""
        record = FakeModel(id="1")
        db_session.add(record)
        db_session.commit()

        record.mark_deleted()
        db_session.commit()
        db_session.refresh(record)

        assert record.deleted_at is not None
        assert isinstance(record.deleted_at, datetime)

    def test_mark_deleted_uses_utcnow(self, db_session: Session) -> None:
        """mark_deleted() should use a timestamp close to utcnow."""
        record = FakeModel(id="1")
        db_session.add(record)
        db_session.commit()

        # TODO: migrate to datetime.now(UTC) when the codebase standardizes on aware datetimes
        before = datetime.utcnow()
        record.mark_deleted()
        after = datetime.utcnow()
        db_session.commit()
        db_session.refresh(record)

        assert before <= record.deleted_at <= after

    def test_not_deleted_filters_deleted_records(self, db_session: Session) -> None:
        """not_deleted() classmethod should return a filter clause excluding deleted records."""
        alive = FakeModel(id="alive")
        dead = FakeModel(id="dead")
        db_session.add_all([alive, dead])
        db_session.commit()

        dead.mark_deleted()
        db_session.commit()

        results = db_session.execute(select(FakeModel).where(FakeModel.not_deleted())).scalars().all()

        assert len(results) == 1
        assert results[0].id == "alive"

    def test_not_deleted_returns_all_when_none_deleted(self, db_session: Session) -> None:
        """not_deleted() should return all records when none are deleted."""
        db_session.add_all([FakeModel(id="a"), FakeModel(id="b")])
        db_session.commit()

        results = db_session.execute(select(FakeModel).where(FakeModel.not_deleted())).scalars().all()

        assert len(results) == 2

    def test_mark_deleted_is_idempotent(self, db_session: Session) -> None:
        """Calling mark_deleted() twice should not change the original timestamp."""
        record = FakeModel(id="1")
        db_session.add(record)
        db_session.commit()

        record.mark_deleted()
        db_session.commit()
        db_session.refresh(record)
        first_deleted_at = record.deleted_at

        assert first_deleted_at is not None

        # Call mark_deleted() again — timestamp must not change
        record.mark_deleted()
        db_session.commit()
        db_session.refresh(record)

        assert record.deleted_at == first_deleted_at

    def test_not_deleted_returns_empty_when_all_deleted(self, db_session: Session) -> None:
        """not_deleted() should return no records when all are deleted."""
        r1 = FakeModel(id="a")
        r2 = FakeModel(id="b")
        db_session.add_all([r1, r2])
        db_session.commit()

        r1.mark_deleted()
        r2.mark_deleted()
        db_session.commit()

        results = db_session.execute(select(FakeModel).where(FakeModel.not_deleted())).scalars().all()

        assert len(results) == 0


class TestExcludeDeleted:
    def test_exclude_deleted_filters_soft_deleted_rows(self, db_session: Session) -> None:
        """exclude_deleted() should add a filter to exclude deleted rows."""
        alive = FakeModel(id="alive")
        dead = FakeModel(id="dead")
        db_session.add_all([alive, dead])
        db_session.commit()

        dead.mark_deleted()
        db_session.commit()

        query = exclude_deleted(select(FakeModel), FakeModel)
        results = db_session.execute(query).scalars().all()

        assert len(results) == 1
        assert results[0].id == "alive"

    def test_exclude_deleted_composes_with_existing_filters(self, db_session: Session) -> None:
        """exclude_deleted() should compose with other query filters."""
        db_session.add_all(
            [
                FakeModel(id="keep"),
                FakeModel(id="other"),
            ]
        )
        db_session.commit()

        query = select(FakeModel).where(FakeModel.id == "keep")
        query = exclude_deleted(query, FakeModel)
        results = db_session.execute(query).scalars().all()

        assert len(results) == 1
        assert results[0].id == "keep"

    def test_exclude_deleted_with_all_deleted(self, db_session: Session) -> None:
        """exclude_deleted() should return empty when all matching rows are deleted."""
        r = FakeModel(id="only")
        db_session.add(r)
        db_session.commit()

        r.mark_deleted()
        db_session.commit()

        query = exclude_deleted(select(FakeModel), FakeModel)
        results = db_session.execute(query).scalars().all()

        assert len(results) == 0
