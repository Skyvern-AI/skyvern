from skyvern.forge.sdk.event.factory import _EventMetrics


class _FakeHistogram:
    def __init__(self) -> None:
        self.samples: list[tuple[float, dict]] = []

    def record(self, amount: float, attributes: dict | None = None) -> None:
        self.samples.append((amount, attributes))


def test_record_emits_histogram_sample_tagged_by_event_type() -> None:
    hist = _FakeHistogram()
    metrics = _EventMetrics(histogram=hist)

    metrics.record("type_text", 0.25)
    metrics.record("move_cursor", 0.5)

    assert hist.samples == [
        (0.25, {"event_type": "type_text"}),
        (0.5, {"event_type": "move_cursor"}),
    ]
