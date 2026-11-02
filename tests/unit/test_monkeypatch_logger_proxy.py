import pytest
import structlog
from structlog.testing import capture_logs


@pytest.mark.parametrize("wrapper_class", [None, structlog.BoundLogger])
def test_undoing_a_monkeypatched_logger_method_does_not_strand_it_on_the_proxy(
    wrapper_class: type[structlog.BoundLogger] | None,
) -> None:
    saved = structlog.get_config()
    if wrapper_class is not None:
        # BoundLogger hands out functools.partial objects rather than bound methods.
        structlog.configure(wrapper_class=wrapper_class)
    log = structlog.get_logger()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(log, "info", lambda *_a, **_kw: None)
    try:
        # setup_logger() installs a new processors list; a stranded method would keep logging through the old one.
        structlog.configure(processors=list(saved["processors"]))
        with capture_logs() as logs:
            log.info("after-undo")
    finally:
        structlog.configure(**saved)

    assert [entry["event"] for entry in logs] == ["after-undo"]


def test_an_inner_undo_keeps_the_outer_patch_on_the_same_logger_method() -> None:
    log = structlog.get_logger()
    calls: list[str] = []

    def record_outer(_logger: object, _method: str, event_dict: dict) -> dict:
        calls.append(f"outer:{event_dict['event']}")
        raise structlog.DropEvent

    # A real bound logger's method, which cleanup cannot tell apart from one the proxy resolved by its type.
    outer_sink = structlog.wrap_logger(None, processors=[record_outer]).bind()
    with pytest.MonkeyPatch.context() as outer:
        outer.setattr(log, "info", outer_sink.info)
        with pytest.MonkeyPatch.context() as inner:
            inner.setattr(log, "info", lambda event, **_kw: calls.append(f"inner:{event}"))
            log.info("first")
        log.info("second")

    assert calls == ["inner:first", "outer:second"]
