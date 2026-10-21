"""HTTP range requests for downloads, whichever server an app is built on.

A player seeks by asking for bytes from an offset (`Range: bytes=N-`): the
`<audio>` element, ExoPlayer and AVPlayer all do, and without a 206 answer
they either cannot seek or download the whole file first. aiohttp's
FileResponse does this itself (Icom, Flex); http.server does not (NB and
Radio), so `parse_range` is the part every server needs, and
`file_headers` the headers that go with it.

Only a single range is served. A multi-range request gets the whole file
(200), which RFC 9110 14.2 allows: no player asks for more than one.
"""
from __future__ import annotations

import re
import urllib.parse

_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


class RangeNotSatisfiable(ValueError):
    """416: the range starts past the end of the file."""


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """(first, last) byte, inclusive, of the one range asked for; None for
    the whole file (no header, or one this ignores). RangeNotSatisfiable
    when the range cannot be served from a file of `size` bytes."""
    if not header:
        return None
    m = _RANGE.match(header.strip().replace(" ", ""))
    if not m:
        return None                                   # several ranges, or another unit: the whole file
    first, last = m.groups()
    if first == "" and last == "":
        return None
    if first == "":
        n = int(last)                                 # the last n bytes
        if n == 0 or size == 0:
            raise RangeNotSatisfiable(f"bytes=-{n} of {size}")
        return max(0, size - n), size - 1
    a = int(first)
    if a >= size:
        raise RangeNotSatisfiable(f"bytes={a}- of {size}")
    b = size - 1 if last == "" else min(int(last), size - 1)
    if b < a:
        return None                                   # malformed: ignored, as RFC 9110 says
    return a, b


def disposition(filename: str, download: bool) -> str:
    """Content-Disposition naming the file: `attachment` to save it,
    `inline` for a player. ASCII fallback plus RFC 5987 for anything else."""
    ascii_name = filename.encode("ascii", "replace").decode().replace('"', "")
    return (f'{"attachment" if download else "inline"}; filename="{ascii_name}"; '
            f"filename*=UTF-8''{urllib.parse.quote(filename)}")


def file_headers(size: int, ctype: str, filename: str, download: bool, rng: tuple[int, int] | None) -> tuple[int, dict]:
    """(status, headers) for a file of `size` bytes and the range parsed
    from the request (None: the whole file)."""
    h = {"Content-Type": ctype, "Accept-Ranges": "bytes", "Content-Disposition": disposition(filename, download),
         "Cache-Control": "no-store"}
    if rng is None:
        h["Content-Length"] = str(size)
        return 200, h
    a, b = rng
    h["Content-Range"] = f"bytes {a}-{b}/{size}"
    h["Content-Length"] = str(b - a + 1)
    return 206, h
