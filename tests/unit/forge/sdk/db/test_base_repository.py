from unittest.mock import MagicMock

from sqlalchemy.exc import OperationalError

from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.protocols import RunReader, TaskReader, WorkflowParameterReader, WorkflowReader
from skyvern.forge.sdk.db.utils import serialize_proxy_location
from skyvern.schemas.runs import GeoTarget, ProxyLocation


class TestSerializeProxyLocation:
    def test_none(self):
        assert serialize_proxy_location(None) is None

    def test_geo_target(self):
        geo = GeoTarget(country="US", state="CA")
        result = serialize_proxy_location(geo)
        assert result is not None
        assert "US" in result

    def test_enum(self):
        result = serialize_proxy_location(ProxyLocation.RESIDENTIAL)
        assert result == "RESIDENTIAL"


class TestBaseRepository:
    def test_init_with_defaults(self):
        mock_session = MagicMock()
        repo = BaseRepository(session_factory=mock_session, debug_enabled=True)
        assert repo.Session is mock_session
        assert repo.debug_enabled is True

    def test_is_retryable_error_default(self):
        mock_session = MagicMock()
        repo = BaseRepository(session_factory=mock_session)
        error = OperationalError("statement", {}, Exception("server closed the connection unexpectedly"))
        assert repo.is_retryable_error(error) is False  # default returns False

    def test_is_retryable_error_custom(self):
        mock_session = MagicMock()

        def custom_fn(e):
            return "closed" in str(e).lower()

        repo = BaseRepository(session_factory=mock_session, is_retryable_error_fn=custom_fn)
        error = OperationalError("statement", {}, Exception("server closed the connection"))
        assert repo.is_retryable_error(error) is True


class TestProtocols:
    def test_protocols_importable(self):
        """Protocols should be importable and runtime-checkable."""
        assert TaskReader is not None
        assert WorkflowReader is not None
        assert WorkflowParameterReader is not None
        assert RunReader is not None
