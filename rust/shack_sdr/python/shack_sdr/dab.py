"""What an app needs to run the `dab` decoder (RADIO-SDR-PLAN §8.5 M3):
the Band III blocks, the IQ the session needs, whether a receiver's span
holds a block, and the codes the decoder's feed uses.

    from shack_sdr import Host, dab
    centre = dab.block_centre("11D")                 # 222_064_000
    why = dab.span_check(centre, rx_centre_hz, rx_rate_hz)   # None, or "outside_span"
    sid = host.start("dab", {"service": "C221", "block": "11D"}, "iq", dab.IQ_RATE)
    host.feed(sid, iq, t0, index)                    # complex64 centred on the block
    host.status(sid)["meta"]                         # the feed: sync, ensemble, services, playing, why
    host.configure(sid, {"service": "C3A7"})         # live
    host.audio(sid, rate=12_000, channels=1)         # the playing service's sound

The session wants IQ centred on the block at `IQ_RATE` (2.048 MS/s, the
DAB elementary rate) or any higher rate, which it resamples (a Pluto's
2.4 MS/s costs about 3 ms of CPU per 96 ms frame more); below it the
start is refused, `DecoderError("bad_input", "rate")`. Whether the span
holds the block is the engine's to judge before it starts a session or
when the dial moves: `span_check` says, with the existing code
`outside_span` (and `Host.set_outside` keeps the session).

Codes, never sentences (the app words them from its catalogue; the README
suggests English for each): the feed's `why` is `no_signal`, `no_sync`,
`fic_errors`, `service_not_found`, `needs_codec:faad2`, `codec_config`,
`codec_open` or `unsupported_protection`; a service's own `why` is
`needs_codec:faad2` when it is DAB+ and faad2 is missing.
"""

from __future__ import annotations

from . import _native

#: The rate the session takes as it is; higher rates are resampled.
IQ_RATE = 2_048_000
#: The block's occupied width (1536 carriers of 1 kHz).
BANDWIDTH_HZ = 1_536_000

#: Band III's blocks and their centre frequencies (Hz), 5A to 13F and the
#: three N blocks: the Wiesbaden plan's names.
BLOCKS: dict[str, int] = dict(_native.dab_blocks())

#: The feed's `why` codes, and a service's.
WHY_CODES = ("no_signal", "no_sync", "fic_errors", "service_not_found", "needs_codec:faad2", "codec_config",
             "codec_open", "unsupported_protection")


def block_centre(name: str) -> int | None:
    """The centre of a Band III block by name ("11D", any case)."""
    return BLOCKS.get(name.strip().upper())


def block_at(freq_hz: float) -> str | None:
    """The block centred within 8 kHz of `freq_hz` (blocks sit on a 16 kHz
    raster), or None."""
    for name, centre in BLOCKS.items():
        if abs(centre - freq_hz) <= 8_000:
            return name
    return None


def span_check(centre_hz: float, rx_centre_hz: float, rx_rate_hz: float, *, usable: float = 0.9) -> str | None:
    """None when a receiver tuned to `rx_centre_hz` at `rx_rate_hz` holds
    the whole block at `centre_hz` within the `usable` part of its band
    (receivers roll off at their edges); otherwise "outside_span", the code
    `api/decoders` already answers with."""
    half = usable * rx_rate_hz / 2.0
    lo, hi = centre_hz - BANDWIDTH_HZ / 2.0, centre_hz + BANDWIDTH_HZ / 2.0
    if lo < rx_centre_hz - half or hi > rx_centre_hz + half:
        return "outside_span"
    return None
