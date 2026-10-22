"""shack_rec: recordings made and kept on the OpenHamShack controller, not
on the device that asked for them (Brian, 12/10/2026).

    rec = shack_rec.Recordings("/data/recordings", shack_rec.Limits.from_env(os.environ),
                               app="icom", kinds=("audio",))
    r = rec.start_audio(12_000, 1, {"freqHz": 144_300_000, "mode": "USB", "station": "GM4BRI"})
    unsubscribe = tap(r.push)          # the app's receive audio, s16le
    r.on_close(unsubscribe)
    rec.stop("audio"); rec.listing(); rec.file(id, "data"); rec.delete(id)

`flac` is the encoder (why FLAC: its docstring), `store` the folder with
its disk-use limit and free-space floor, `audio` one audio recording,
`recordings` the rules an app's API answers by, `http` range requests.
Every app with recordings exposes the same API (README.md).
"""

from __future__ import annotations

from .audio import AudioRecorder
from .flac import FlacWriter
from .http import RangeNotSatisfiable, file_headers, parse_range
from .recordings import Limits, Recordings
from .store import GB, Refused, Store, size_text

__version__ = "0.2.0"

__all__ = ["AudioRecorder", "FlacWriter", "GB", "Limits", "RangeNotSatisfiable", "Recordings", "Refused", "Store",
           "file_headers", "parse_range", "size_text"]
