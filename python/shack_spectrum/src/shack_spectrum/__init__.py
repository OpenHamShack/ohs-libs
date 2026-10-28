"""shack_spectrum: the server side of spectrum/1 (CLIENT-UI-PLAN.md §2).

One waterfall feed for every OpenHamShack app: the binary row
(`shack_spectrum.row`), per-client decimation and quantisation
(`shack_spectrum.reduce`), the server's levels window with its hysteresis
(`shack_spectrum.levels`) and latest-wins sending (`shack_spectrum.sender`).
The JSON control messages are spec/schemas/spectrum.schema.json; the
byte-exact vectors every client decoder runs are spec/vectors/spectrum/.

    lo, col_hz, db = reduce(src_db, src_lo_hz, bin_hz, cols=1280, mode="peak")
    if window.update(src_db, t_ms):
        ws.send_json(window.message("main"))
    sender.offer(client, Row(0, 0, t_ms, lo, col_hz, quantise(db, window.floor, window.step())))
"""

from __future__ import annotations

from .levels import LevelsWindow
from .reduce import crop, dequantise, quantise, reduce
from .row import Row, RowError, SpectrumError, decode, encode
from .sender import LatestWins

__version__ = "0.2.0"

__all__ = ["LatestWins", "LevelsWindow", "Row", "RowError", "SpectrumError", "crop", "decode", "dequantise", "encode", "quantise",
           "reduce"]
