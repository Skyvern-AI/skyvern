"""Targeted tests for shared unit-test helpers."""

from tests.unit.helpers import DummyLogger


def test_dummy_logger_info_captures_mixed_kwargs() -> None:
    """DummyLogger.info must accept kwargs of arbitrary types, not only dicts."""
    logger = DummyLogger()
    logger.info(
        "test_event",
        str_val="hello",
        int_val=42,
        none_val=None,
        list_val=[1, 2, 3],
        dict_val={"nested": True},
    )

    assert len(logger.events) == 1
    event, captured = logger.events[0]
    assert event == "test_event"
    assert captured["str_val"] == "hello"
    assert captured["int_val"] == 42
    assert captured["none_val"] is None
    assert captured["list_val"] == [1, 2, 3]
    assert captured["dict_val"] == {"nested": True}


def test_dummy_logger_noop_methods_do_not_raise() -> None:
    """warning, exception, and debug should be no-ops and never raise."""
    logger = DummyLogger()
    logger.warning("warn", extra="data")
    logger.exception("exc", exc_info=True)
    logger.debug("debug", detail="verbose")
    assert logger.events == []
