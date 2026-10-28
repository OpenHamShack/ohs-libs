# shack-spectrum

The server side of **spectrum/1** (CLIENT-UI-PLAN.md §2): one waterfall
feed for every OpenHamShack app, replacing the five formats the apps send
today. Clients (Kotlin, Swift, JS) decode it; this package encodes it and
is the reference the vectors in `spec/vectors/spectrum/` were written by.

| Module | What |
|---|---|
| `shack_spectrum.row` | `Row`, `encode(row)`, `decode(bytes)`: the 32-byte little-endian header and n uint8/uint16 columns of §2.3. `decode` raises `RowError` with `code` `magic`, `version` or `length` rather than draw a mis-read row. |
| `shack_spectrum.reduce` | `reduce(src_db, src_lo_hz, bin_hz, cols, lo_hz, hi_hz, mode)`: one client's columns, `peak` or `mean` (power average), cropped to whole source bins inside the span, never more columns than bins. `quantise`/`dequantise`: dB to row values and back. |
| `shack_spectrum.levels` | `LevelsWindow`: the per-stream window (floor = 20th percentile - 10 dB, top = max(floor + 60, 99.7th percentile + 6)), moved only after either edge has been more than 3 dB out for 2 s. `message(stream, depth)` is the levels JSON. |
| `shack_spectrum.sender` | `LatestWins(send)`: at most one pending row per client per stream; a replaced row sets `gap` on the next. No socket: the transport calls `drained(client)`. |

## Decisions a port must copy

- Column boundaries are integer bin indices (`k*m//n`), never float
  frequencies, so every language picks the same bins.
- `quantise` rounds half up, `floor(x + 0.5)`.
- `dbStep` is the window over `2**depth` steps (96 dB is 0.375 dB, the
  plan's example).
- Percentiles are linear between closest ranks (numpy's default).
- The sequence is stamped when a row is offered, so a dropped row shows
  as a jump as well as `gap`.

The JSON control messages (subscribe, stream, levels, error) are
`spec/schemas/spectrum.schema.json`. `tools/gen-client-vectors.py`
rewrites the vectors from this package; run it after changing behaviour
and review the diff, since every client is tested against them.
