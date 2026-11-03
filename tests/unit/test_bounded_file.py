import hashlib

from skyvern.forge.sdk.artifact.storage.bounded_file import BoundedFileReader


def _write(path, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)


def test_reads_exactly_length_and_ignores_later_growth(tmp_path) -> None:
    """Snapshot semantics: the reader yields exactly [0, length) even if the file grows afterward,
    so a per-step upload never follows the still-growing recording's EOF."""
    p = tmp_path / "rec.webm"
    _write(p, b"A" * 100)
    reader = BoundedFileReader(str(p), 100)

    # File keeps growing after the snapshot was taken.
    with open(p, "ab") as f:
        f.write(b"B" * 900)

    out = reader.read()
    assert out == b"A" * 100
    assert reader.read() == b""  # nothing past the bound
    reader.close()


def test_chunked_reads_are_byte_identical_to_prefix(tmp_path) -> None:
    p = tmp_path / "rec.webm"
    full = bytes((i * 37 + 11) % 256 for i in range(5000))
    _write(p, full)
    length = 4096
    reader = BoundedFileReader(str(p), length)

    chunks = []
    while True:
        c = reader.read(512)
        if not c:
            break
        assert len(c) <= 512
        chunks.append(c)
    reader.close()

    joined = b"".join(chunks)
    assert len(joined) == length
    assert joined == full[:length]
    assert hashlib.sha256(joined).hexdigest() == hashlib.sha256(full[:length]).hexdigest()


def test_seek_and_tell_support_size_probe_and_retry_rewind(tmp_path) -> None:
    """upload_fileobj probes size via seek(0, 2)/tell and rewinds via seek(0) on retry."""
    p = tmp_path / "rec.webm"
    _write(p, b"Z" * 800)
    length = 500
    reader = BoundedFileReader(str(p), length)

    assert reader.seek(0, 2) == length  # size probe returns the bound, not the file size
    assert reader.tell() == length
    reader.seek(0)  # rewind for retry
    assert reader.tell() == 0

    first = reader.read(length)
    assert first == b"Z" * length
    reader.seek(0)
    assert reader.read(length) == first  # replay after rewind is identical
    reader.close()


def test_read_never_exceeds_bound_even_with_large_request(tmp_path) -> None:
    p = tmp_path / "rec.webm"
    _write(p, b"Q" * 10_000)
    reader = BoundedFileReader(str(p), 1234)
    out = reader.read(1_000_000)
    assert out == b"Q" * 1234
    reader.close()
