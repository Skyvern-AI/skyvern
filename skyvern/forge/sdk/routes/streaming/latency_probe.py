from opentelemetry import metrics

_LATENCY_BUCKETS_SECONDS = [0.001, 0.002, 0.005, 0.01, 0.02, 0.03, 0.045, 0.06, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0]
MAX_SESSIONS = 1024
_pending_inputs: dict[str, float] = {}
# dict, not set: the cap must evict the oldest session, not an arbitrary one.
_recording: dict[str, None] = {}

_meter = metrics.get_meter("skyvern.live_view")
input_to_frame_seconds = _meter.create_histogram(
    "skyvern.live_view.input_to_frame_seconds",
    unit="s",
    description="Input message received by the API -> next screencast frame received by the API",
    explicit_bucket_boundaries_advisory=_LATENCY_BUCKETS_SECONDS,
)


def note_input(browser_session_id: str, received_at: float) -> None:
    if browser_session_id not in _pending_inputs and len(_pending_inputs) >= MAX_SESSIONS:
        _pending_inputs.pop(next(iter(_pending_inputs)))
    _pending_inputs[browser_session_id] = received_at


def note_frame(browser_session_id: str, received_at: float) -> None:
    input_received_at = _pending_inputs.pop(browser_session_id, None)
    if input_received_at is None:
        return
    input_to_frame_seconds.record(
        received_at - input_received_at,
        {"recording": "on" if browser_session_id in _recording else "off"},
    )


def set_recording(browser_session_id: str, active: bool) -> None:
    if active:
        if browser_session_id not in _recording and len(_recording) >= MAX_SESSIONS:
            _recording.pop(next(iter(_recording)))
        _recording[browser_session_id] = None
    else:
        _recording.pop(browser_session_id, None)


def forget(browser_session_id: str) -> None:
    """Every teardown path reaches here, so it also clears the recording gauge an abrupt
    disconnect would otherwise leave latched on."""
    _pending_inputs.pop(browser_session_id, None)
    _recording.pop(browser_session_id, None)
