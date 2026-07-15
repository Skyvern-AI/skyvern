"""Tests for persistent-browser-session VNC metadata."""

from datetime import datetime

from sqlalchemy import Integer, String

from skyvern.config import Settings
from skyvern.forge.sdk.db.models import PersistentBrowserSessionModel
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession


def test_persistent_browser_session_schema_defaults_vnc_metadata() -> None:
    session = PersistentBrowserSession(
        persistent_browser_session_id="pbs_123",
        organization_id="org_123",
        created_at=datetime(2026, 1, 1),
        modified_at=datetime(2026, 1, 1),
    )

    assert session.display_number is None
    assert session.vnc_port is None
    assert session.interactor == "agent"


def test_persistent_browser_session_model_exposes_nullable_vnc_metadata() -> None:
    columns = PersistentBrowserSessionModel.__table__.columns

    assert isinstance(columns["display_number"].type, Integer)
    assert columns["display_number"].nullable is True
    assert isinstance(columns["vnc_port"].type, Integer)
    assert columns["vnc_port"].nullable is True
    assert isinstance(columns["interactor"].type, String)
    assert columns["interactor"].nullable is True
    assert str(columns["interactor"].server_default.arg) == "'agent'"


def test_default_display_setting_is_integer_99() -> None:
    default_display = Settings.model_fields["SKYVERN_DEFAULT_DISPLAY"].default

    assert default_display == 99
    assert isinstance(default_display, int)
