from __future__ import annotations

import os
from typing import Self


class BoundedFileReader:
    """A seekable, read-only file-like view over exactly ``[0, length)`` of ``path``.

    ``length`` is captured by the caller before the read (a snapshot of the recording's current
    size), so the view never follows the file's still-growing EOF. It exposes the ``read``/``seek``/
    ``tell`` surface ``upload_fileobj`` needs: a size probe (``seek(0, 2)`` returns ``length``) and a
    retry rewind (``seek(0)``).
    """

    def __init__(self, path: str, length: int) -> None:
        self._length = max(0, int(length))
        # The reader owns this handle for the whole streamed upload (including retry rewinds), so it
        # is deliberately not a with-block; close() / the context manager release it.
        self._fh = open(path, "rb")  # noqa: SIM115
        self._pos = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self._length - self._pos
        if remaining <= 0:
            return b""
        want = remaining if size is None or size < 0 else min(size, remaining)
        data = self._fh.read(want)
        self._pos += len(data)
        return data

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            target = offset
        elif whence == os.SEEK_CUR:
            target = self._pos + offset
        elif whence == os.SEEK_END:
            target = self._length + offset
        else:
            raise ValueError(f"invalid whence: {whence}")
        target = max(0, min(target, self._length))
        self._fh.seek(target)
        self._pos = target
        return self._pos

    def tell(self) -> int:
        return self._pos

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    @property
    def closed(self) -> bool:
        return self._fh.closed

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
