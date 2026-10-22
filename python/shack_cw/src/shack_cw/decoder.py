"""The CW (Morse) decoder: text from a radio's audio, at whatever speed the
sender keys and whatever rate the audio comes at.

Moved from QO-100 NB (openhamshack-nb ohscore/modes/cw.py, main 47be138) so that
every app with a receiver - NB and Radio, Icom, next Flex - runs the same
decoder (GUIDE: shared components, one of each). Unlike FT8/PSK/RTTY there
is no fixed symbol clock to lock to: a human (or a keyer with its own idea
of WPM) sets the speed, and it can drift mid-transmission. So the decoder
learns the speed from what it hears and keeps following it, rather than
assuming a fixed speed from a menu; Decoder's docstring has the how and
the why, and shack_cw.bench the measurements.

In NB it ran at 12 kHz only. Every constant in it is in seconds or Hz, so
the rate is now whatever feed() is given; at 12 kHz the arithmetic is the
same, operation for operation, and the bench prints NB's numbers exactly.
"""

from __future__ import annotations

from collections import deque
from typing import Callable

import numpy as np

from .i18n import _
from .morse import CODE_TO_CHAR, CODE_TO_PROSIGN

FRAME_S = 0.005
#: The tone, then the noise references either side of it, in Hz from the
#: tone: inside the default 500 Hz CW filter, and clear of the tone's
#: keying sidebands. With a narrower filter they are in its skirts, which
#: the decoder's learnt ratio absorbs (see Decoder._frame). A front end
#: outside the decoder (shack_cw.skim) sums its frames at these offsets.
REF_OFFSETS = (0.0, -225.0, -175.0, -125.0, 125.0, 175.0, 225.0)
_HIST = 8   # frames of history amplitudes() keeps: the longest window


def window_frames(dot_s: float) -> int:
    """The coherent window, in 5 ms frames, for a dot `dot_s` long: about a
    quarter of a dot (25 ms at 12 WPM, ~40 Hz wide), one frame at least and
    eight at most."""
    return int(min(_HIST, max(1, round(0.25 * dot_s / FRAME_S))))


def amplitudes(c, m: int, hist=None, scale: float = 1.0):
    """From per-frame complex sums to what Decoder.feed_frames takes.

    `c` is complex, one row per 5 ms frame: the window-weighted sum of the
    signal at the tone and at each REF_OFFSETS reference (last axis, 7), with
    every frequency's phase carried on across frames so the sums stay
    coherent. Any axes between the two are independent channels: a
    skimmer's filter bank does a whole band's channels at once, grouped by
    `m` (window_frames). `hist` is what the last call returned (None to
    start); `scale` only divides the amplitudes (feed() passes its frame
    length, so its numbers are what they always were - the decoder judges
    ratios, so the scale does not change what it prints).

    Returns (tone, quiet, loud, hist): the tone's amplitude, the noise
    beside it on the quieter side and on the louder side, each one value
    per frame (and per channel).

    One function for both callers (Decoder.feed and the skimmer), so the
    bench measures what the skimmer runs; a copy in the skimmer would have
    drifted from the decoder the first time either changed."""
    c = np.asarray(c)
    lead = c.shape[1:]
    if hist is None:
        hist = (np.zeros((0,) + lead, np.complex128), np.zeros((0,) + lead))
    chist, ahist = hist
    # Coherent window: m frames, about a quarter of a dot. Then a moving
    # average of the amplitude over as many again, to steady it.
    cc = np.concatenate([chist, c])
    cs = np.concatenate([np.zeros((1,) + lead), np.cumsum(cc, axis=0)])
    idx = np.arange(len(chist), len(cc)) + 1
    amp = np.abs(cs[idx] - cs[np.maximum(0, idx - m)]) / (m * scale)
    aa = np.concatenate([ahist, amp])
    sa = np.concatenate([np.zeros((1,) + lead), np.cumsum(aa, axis=0)])
    j = np.arange(len(ahist), len(aa)) + 1
    sm = (sa[j] - sa[np.maximum(0, j - m)]) / np.minimum(m, j).reshape((-1,) + (1,) * len(lead))
    # The noise next to the tone, through the same filter: the median of
    # the three references below the tone or of the three above, whichever
    # is quieter. One other signal on one of them moves a median little,
    # and a neighbour 200 Hz away sits on three references, all on its
    # side, and the median of all six went up and down with its keying -
    # the floor with it, so the tone bin, quiet, stood over a floor learnt
    # in the neighbour's key-down, and its gaps were read as Morse (bench
    # neighbour-200: 120-280 characters a minute of E and I). Either side
    # alone is still three references.
    lo = np.sort(sm[..., 1:4], axis=-1)[..., 1]
    hi = np.sort(sm[..., 4:7], axis=-1)[..., 1]
    return sm[..., 0], np.minimum(lo, hi), np.maximum(lo, hi), (cc[-_HIST:], aa[-_HIST:])


class Decoder:
    """Morse from a receiver's audio, at whatever speed the sender keys.

    Four stages, each the established answer to one way a simple
    threshold decoder fails (the first version of this class, kept in git
    history (openhamshack-nb), read QO-100's lower beacon at 10 dB SNR as strings of
    bracketed unknowns; shack_cw.bench measures every case below):

    1. Envelope: the tone is mixed to 0 Hz and summed coherently over a
       window a quarter of a dit long (25 ms at 12 WPM, ~40 Hz wide) - a
       filter matched to the keying speed, as fldigi's CW decoder does
       with its speed-following FIR ("matched filter") and moving-average
       "bit filter" (W1HKJ, fldigi src/cw_rtty/cw.cxx). The 5 ms Goertzel
       frames it replaces were 200 Hz wide: 7 dB more noise for nothing.
       Each 5 ms frame is weighed by a Hann window two frames long, so a
       strong station a few hundred Hz away stays out of it (see _feed).
    2. AFC: a narrow filter needs the tone where it is, not where the
       pitch says it should be (a beacon lock a few Hz out, an operator who
       tuned by ear, a drifting LNB), so the strongest steady line within
       AFC_HZ of the pitch is tracked from each block's spectrum.
    3. Levels: mark/space is decided against a threshold half-way between
       the noise floor and the signal level (a peak hold that follows QSB
       down), with hysteresis - two thresholds, one to enter a mark and a
       lower one to leave it, as fldigi's upper/lower thresholds. The floor
       is measured beside the tone, at the same instant, so the receiver's
       AGC moving the noise between words cannot fool it (see _frame), on
       the quieter side, so a neighbour on the other cannot move it. A
       squelch on the tone's signal-to-noise over the last 2.5 s (its 75th
       percentile: keyed CW is up about half the time, noise has no such
       quarter) says whether anything is there at all; below `snr_db` (6
       dB) nothing is printed and no speed is shown. Its earlier form, a
       peak hold over the floor, read real noise - static, a neighbour's
       key clicks - as Morse (Brian's 20 m report of 13/10/2026), and so
       does a frame where the tone rises only with a click beside it.
       shack_cw.corpus measures all of it on recordings of the bands.
    4. Timing: marks are sorted into dots and dashes by two-means
       clustering of the recent marks' lengths (the speed is then the dot
       cluster's mean, and the dash cluster's over three), and gaps into
       element/letter/word by thresholds between the running mean of each
       class, so a sender's own rhythm (heavy dashes, short letter gaps)
       moves the thresholds with it. The ideal ratios are ITU-R M.1677-1's
       1:3 and 1:3:7. A mark far longer than a dash (QO-100's lower beacon
       holds key-down for 12 s every cycle) is a carrier: no element, and
       it teaches the speed nothing - letting it in was what wrecked the
       first letters after every beacon cycle.

    5. Polarity: an F1A beacon (QO-100's lower and upper) is one carrier
       pulled 400 Hz between key-up and key-down, so tuned with its key-UP
       tone on the pitch the decoder hears the keying inside out - a tone
       in every gap, silence in every element - and every word space
       becomes a 7-dot "mark", read as a dash, so it never prints a space
       either: strings of E, T and I (Brian's TV on 10/10/2026, the upper
       beacon at the old bookmark: "EEEXV <SK>EWEITEENEEETEEEE...", which
       cw_bench's beacon-bookmark case prints letter for letter). Real
       Morse has no mark of 5-12 dots and many gaps that long (word
       spaces); keying inside out has the reverse. Whichever the recent
       long runs say (see _polarity), the runs are read that way round.

    A letter that is not in the table is usually two letters whose gap was
    read as an element gap; it is split at its longest internal gap (if
    both halves are letters) before being shown as "*".

    The per-frame loop is over 5 ms frames (200 a second), not samples
    (openhamshack-nb DESIGN.md: per-symbol loops are fine, per-sample ones are
    not). At 48 kHz a frame is 240 samples rather than 60, the same 5 ms.
    """

    FRAME_S = FRAME_S
    AFC_HZ = 100.0          # how far from the pitch the tone may be found
    ON, OFF = 0.5, 0.35     # thresholds, as fractions of the way from floor to signal
    CARRIER_DITS = 8.0      # a mark longer than this many dots is a carrier, not a dash
    HISTORY = 24            # marks the dot/dash clustering looks back over
    ACQ_MARKS = 8           # marks needed before the speed is locked (see _acquire)
    ACQ_MIN = 3             # fewer marks than this are never locked on, even forced
    ACQ_CARRIER_S = 2.0     # while learning the speed, a mark this long is a carrier
    REACQUIRE_S = 5.0       # squelch closed this long: the next signal is learnt afresh
    LONG_DOTS = (5.0, 12.0) # a run this many dots long is a word space, whichever way it is keyed
    LONG_HISTORY = 6        # the long runs _polarity looks back over
    SQ_WINDOW_S = 2.5       # the squelch judges the last this many seconds (see _frame)
    SQ_PCT = 75             # ... by this percentile of the tone over the floor
    SQ_HYST_DB = 1.0        # and closes this much below where it opens
    RATIO_Q = 0.3           # the floor is learnt as this quantile of key-up (see _step)
    RATIO_C = 1.3           # ... times this: noise's median over that quantile (bench 1.27, bands 1.38)
    CLICK_JUMP = 3.0        # a click: the louder side over its last half second by this (see _feed)
    CLICK_OVER = 1.5        # ... and the tone not this far over it

    ENGINES = ("classic", "nn")

    def __new__(cls, *args, engine: str = "classic", **kwargs):
        # Decoder(engine="nn") is the neural engine (shack_cw.nn) behind this
        # front end, so an app chooses it with one setting and keeps every
        # call it makes; imported only when asked for, so the classic engine
        # never loads the weights.
        if engine not in cls.ENGINES:
            raise ValueError(_("unknown CW engine {engine!r}: one of {engines}").format(
                engine=engine, engines=", ".join(cls.ENGINES)))
        if engine == "nn" and cls is Decoder:
            from .nn import NnDecoder
            return super().__new__(NnDecoder)
        return super().__new__(cls)

    def __init__(self, emit: Callable[[dict], None] | None = None, freq: float = 700.0, wpm_hint: float = 20.0,
                 squelch: float = 4.0, afc: bool = True, snr_db: float = 6.0, polarity: bool = True, **_opts):
        self.emit = emit
        self.freq = freq
        self._wpm_hint = wpm_hint
        self._squelch = squelch   # signal/floor amplitude ratio below which nothing is a mark
        self.snr_db = snr_db      # the squelch's threshold (see _frame); may be set while running
        # Whether keying heard inside out is turned over (stage 5): for
        # QO-100's F1A beacons. There are none on HF, where an NCDXF
        # beacon's fading 1 s dashes looked like the long tones of
        # inside-out keying and turned the next station over (17 m in the
        # corpus: "CS3B" read "I*ETV T"); Radio passes False.
        self._polarity_on = polarity
        self._afc = afc
        # Learnt from the first feed() (see there): NB feeds 12 kHz, an
        # Icom radio's LAN audio is 12 kHz too, Flex's 24 kHz, a sound card 48.
        self._rate: float | None = None
        self.frame_len = 0
        self._out: list[dict] = []
        self._ref_off = np.array(REF_OFFSETS)   # the tone and its noise references
        self.reset()

    def reset(self) -> None:
        self._buf = np.zeros(0, dtype=np.float32)
        self._prev = None                # the last frame's samples (each window spans two)
        self._hann = None                # that window, for the frame length in use
        self._f = float(self.freq)       # where the tone is believed to be (AFC)
        self._spec = None                # AFC's averaged spectrum round the pitch
        self._win = None                 # AFC's window, for the block length last seen
        self._phases = np.zeros(len(self._ref_off))   # each frequency's mixer phase, across blocks
        self._hist = None                # amplitudes()' last frames (coherent window, average)
        self._level = 0.0                # the signal level (see feed)
        self._dot = 1.2 / self._wpm_hint
        self._dash = 3 * self._dot
        self._marks: list[float] = []
        self._intra = self._dot          # running means of the three kinds of gap
        self._inter = 3 * self._dot
        self._wordgap = 7 * self._dot
        self._floor = 0.0
        self._ratio = 1.0                # tone-bin noise over reference noise, learnt while key-up
        self._learnt = 0                 # key-up frames it has learnt from
        self._ref = None                 # the references' noise, smoothed
        self._near = None                # the louder side of them, smoothed (see _feed)
        self._open = False
        self._closed = 0                 # frames the squelch has been closed
        n = int(round(self.SQ_WINDOW_S / self.FRAME_S))
        self._snr = deque(maxlen=n)      # each frame's tone over the floor, for the squelch
        self._back = deque(maxlen=n)     # (tone, level, floor) of the same frames, to hear again
        self._stat = 0.0                 # the squelch's statistic (see _feed)
        self._sq_count = 0               # frames since it was worked out
        self._locked = False             # speed learnt (see _acquire)
        self._acq: list[tuple[bool, float]] = []   # runs heard while learning it
        self._last_gap = 0.0
        self._state = False
        self._run = 0                    # frames in the current (committed) run
        self._pending = 0                # frames disagreeing with it
        self._code = ""
        self._gaps: list[float] = []     # element gaps inside the letter in progress
        self._text = ""
        self._letter_done = False
        self._space_done = False
        self._in_carrier = False
        self._inverted = False           # keying heard inside out (see _polarity)
        self._turning = False            # _acquire is learning again the other way round
        self._long: list[bool] = []      # recent long runs: True for a tone, False for a gap

    # ------------------------------------------------------------ front end
    def _track(self, audio: np.ndarray) -> None:
        """AFC: follow the strongest steady line within AFC_HZ of the pitch."""
        if not self._afc or len(audio) < 256:
            return
        # Bins of 6 Hz or finer whatever the rate (2048 points at 12 kHz,
        # as NB always had), or the block's own length if that is longer.
        n = 1 << max(int(np.ceil(np.log2(self._rate / 6.0))), int(np.ceil(np.log2(len(audio)))))
        if self._win is None or len(self._win) != len(audio):
            self._win = np.hanning(len(audio))
        P = np.abs(np.fft.rfft(audio * self._win, n)) ** 2
        df = self._rate / n
        k0 = int((self.freq - self.AFC_HZ) / df)
        k1 = int((self.freq + self.AFC_HZ) / df) + 1
        if k0 < 1 or k1 >= len(P) - 1:
            return
        seg = P[k0 - 1:k1 + 1]
        # About 1.5 s of memory at 75 ms blocks: long enough that the gaps
        # of keyed CW do not matter, short enough to follow a drifting LNB.
        self._spec = seg if self._spec is None or len(self._spec) != len(seg) else 0.95 * self._spec + 0.05 * seg
        s = self._spec
        i = int(np.argmax(s[1:-1])) + 1
        if s[i] < 8 * np.partition(s, len(s) // 2)[len(s) // 2]:   # 8x the median
            return   # no clear line: stay where we are
        if s[i - 1] > s[i] or s[i + 1] > s[i]:
            # The highest bin is the skirt of a line outside the window,
            # not a line in it: a carrier 120 Hz from the pitch, with
            # nothing on the pitch, pulled the filter 100 Hz towards itself.
            return
        a, b, c = s[i - 1], s[i], s[i + 1]
        den = a - 2 * b + c
        frac = 0.5 * (a - c) / den if den else 0.0
        f = (k0 - 1 + i + frac) * df
        self._f += 0.5 * (f - self._f)

    def feed(self, samples: np.ndarray, rate: float) -> list[dict]:
        """Decode a block of mono audio at `rate` samples a second; returns
        the events it produced (also handed to `emit`, if there is one).

        Any block length works. A new rate starts afresh (reset): the
        mixers, the window and the frame length all depend on it, and a
        stream that changes rate has been restarted anyway."""
        rate = float(rate)
        if rate != self._rate:
            self._rate = rate
            self.frame_len = max(1, int(round(self.FRAME_S * rate)))
            self.reset()
        self._out = []
        self._feed(np.asarray(samples, dtype=np.float32))
        out, self._out = self._out, []
        return out

    def _feed(self, audio: np.ndarray) -> None:
        c = self._sums(audio)
        if c is not None:
            self._from_sums(c)

    def _from_sums(self, c: np.ndarray) -> None:
        """Everything after the mixer: what an engine other than this one
        (shack_cw.nn) replaces, behind the same front end."""
        tone, quiet, loud, self._hist = amplitudes(c, self.window_frames, self._hist, scale=self.frame_len)
        self._frames(tone, quiet, loud)

    def _sums(self, audio: np.ndarray) -> np.ndarray | None:
        """AFC, then each whole 5 ms frame's complex sums at the tone and
        the noise references (frames, 7); None if no frame is complete."""
        self._track(audio)
        buf = np.concatenate([self._buf, audio])
        n_frames = len(buf) // self.frame_len
        if n_frames == 0:
            self._buf = buf
            return None
        used = n_frames * self.frame_len
        self._buf = buf[used:]
        fl = self.frame_len
        # Each frame summed against the tone and the noise references at
        # once (one small matrix product); every frequency's phase runs on
        # across frames and blocks, so the sums stay coherent over the
        # window below.
        # A frame is weighed with a Hann window two frames long, centred
        # on it (so each sample counts in two frames, and the weights of
        # neighbouring frames add to one). The plain 5 ms sum it replaces
        # had sinc sidelobes, -13 dB and falling 6 dB an octave: a station
        # 30 dB up and 600 Hz away (Brian's 20 m report: the pitch beside
        # a real signal in a 1500 Hz filter) came through 10 dB over the
        # noise, keyed, and was read. Hann's fall 18 dB an octave from -31.
        if self._prev is None or len(self._prev) != fl:
            self._prev = np.zeros(fl)
        xx = np.concatenate([self._prev, buf[:used].astype(np.float64)])
        self._prev = xx[-fl:]
        x = np.lib.stride_tricks.sliding_window_view(xx, 2 * fl)[::fl][:n_frames]
        f = self._f + self._ref_off
        w = 2 * np.pi * f / self._rate
        i = np.arange(2 * fl)
        if self._hann is None or len(self._hann) != 2 * fl:
            self._hann = 0.5 - 0.5 * np.cos(2 * np.pi * (i + 0.5) / (2 * fl))
        c = x @ (self._hann[:, None] * np.exp(-1j * np.outer(i, w)))   # (frames, freqs)
        # Each window starts a frame before its own frame: the phase is
        # carried from there.
        c *= np.exp(-1j * (self._phases + np.outer(np.arange(n_frames) * fl - fl, w)))
        self._phases = (self._phases + w * used) % (2 * np.pi)
        return c

    @property
    def window_frames(self) -> int:
        """The coherent window, in 5 ms frames, for the speed learnt so far
        (see amplitudes): what a front end outside the decoder passes
        amplitudes() for this decoder's channel."""
        return window_frames(self._dot)

    def feed_frames(self, tone, quiet, loud=None) -> list[dict]:
        """The decoder without its front end: one value per 5 ms frame of
        the tone's amplitude, of the noise beside it on the quieter side
        and on the louder side (`loud`, for the click test; the quieter
        side if not given), all through the same narrow filter. Returns
        the events, as feed() does.

        feed() is this behind its own front end (the mixer, frames and
        AFC above). A front end that already has every channel's amplitude
        - a skimmer's filter bank over a whole band - drives one Decoder a
        channel through this instead, and the squelch, timing and output
        cost only this loop (shack_cw.bench reports it per channel). Call
        it with whole blocks; the rate is fixed by FRAME_S."""
        if self._rate is None:
            # Nothing here depends on the audio rate; the frame length is
            # only feed()'s. Marked so a later feed() still starts afresh.
            self._rate = -1.0
        self._out = []
        self._frames(tone, quiet, loud)
        out, self._out = self._out, []
        return out

    def _frames(self, tone, quiet, loud=None) -> None:
        tone = np.asarray(tone, dtype=np.float64)
        quiet = np.asarray(quiet, dtype=np.float64)
        loud = quiet if loud is None else np.asarray(loud, dtype=np.float64)
        # The signal level is a peak hold that halves in eight dots: slow
        # enough to bridge a word space (7 dots; a five-dot sliding maximum
        # let the level fall to the noise inside one, and the noise was
        # read as Morse), fast enough to follow QSB down (7.5 dB/s at 12
        # WPM against a fade of 12 dB in 3 s).
        decay = 0.5 ** (self.FRAME_S / (8 * self._dot))
        for e, r, nr in zip(tone.tolist(), quiet.tolist(), loud.tolist()):
            # The squelch's percentile every 75 ms (15 frames), whatever
            # the block length: a sort per frame would be most of the cost.
            self._sq_count += 1
            if self._sq_count >= 15 and len(self._snr) >= 40:
                self._sq_count = 0
                self._stat = float(np.percentile(self._snr, self.SQ_PCT))
            self._level = max(e, self._level * decay)
            # A click: the louder side jumps (CLICK_JUMP over its usual
            # level) and the tone does not stand CLICK_OVER over it. Key
            # clicks and static are broadband, so they land on the tone and
            # beside it at once; beside a strong station on 30 m (the pitch
            # 600 Hz from it in a 1500 Hz filter) every false mark stood
            # level with the louder side (median 1.0, 90 % under 1.44),
            # where its real marks, heard on the pitch, stood 5x over it or
            # more. The jump keeps a steady carrier beside the tone (bench
            # beacon-beside) from counting: that is not a click, and the
            # tone beside it is often no stronger than it.
            # The usual level is a running median (2 % a frame up or down):
            # a running mean was lifted by the static itself - eight
            # crashes a second, 32 dB up (bench qrn at 12 dB) - until they
            # no longer counted as jumps and were read as dots.
            if self._near is None:
                self._near = nr
            self._near *= 1.02 if nr > self._near else 1 / 1.02
            click = nr > self.CLICK_JUMP * self._near and e < self.CLICK_OVER * nr * self._ratio * self.RATIO_C
            self._frame(e, self._level, r, click)

    def _frame(self, e: float, s: float, ref: float, click: bool = False) -> None:
        # The floor is the noise *beside* the tone, scaled by what the noise
        # on the tone has been to it while key-up. The receiver's AGC acts
        # on both at the same instant, so the floor follows the AGC exactly
        # - after the beacon's 12 s carrier the medium AGC raises the noise
        # 20 dB a second, which a floor learnt from past key-up (the first
        # versions of this) lagged behind until the noise itself cleared
        # the squelch. And the floor cannot climb onto the signal: a QSB
        # trough, or a long carrier, is "key-up" to a floor that follows
        # key-up, and took the squelch down with it.
        # Averaged over ~50 ms (the references are as noisy as the tone's
        # own envelope; a floor that dipped with them opened the squelch
        # on noise), which still follows 20 dB/s of AGC within 12 %.
        self._ref = ref if self._ref is None else self._ref + 0.1 * (ref - self._ref)
        fl = self._ratio * self.RATIO_C * self._ref
        self._floor = fl
        # The squelch: is a keyed signal here at all? Judged on how the
        # tone has stood over the floor through the last SQ_WINDOW_S (2.5
        # s): the 75th percentile of the ratio, frame by frame. The
        # receiver's AGC moves tone and floor together, so the ratio is the
        # signal-to-noise whatever the AGC is doing. Keyed CW is up 40-50 %
        # of the time, so its 75th percentile sits on the marks; noise has
        # no such quarter. On the real bands (shack_cw.corpus) noise's
        # statistic never passed 1.75 (5 dB) - 80 m static at night
        # included - where a beacon 3 dB over the noise in 2.5 kHz reached
        # 6 (16 dB). It opens at `snr_db` (6 dB) and closes SQ_HYST_DB
        # below that.
        # The peak-hold test that was the whole squelch before (the level
        # `squelch` times over the floor, closing at 0.6 of that) stays as
        # a second condition. Alone it was Brian's 20 m report of
        # 13/10/2026 ("27.4 WPM .2 NE TU A H OW TEE I* NE H E M..." from
        # noise): real noise is not the bench's Gaussian - static crashes,
        # a neighbour's key clicks - and its peaks held the level over 4x
        # the floor a quarter of the time on 80 m, the squelch open, and
        # every flicker over half-way was a dot.
        # A click counts as the floor, not as tone: eight crashes a second,
        # each smeared over a few frames, are a quarter of the window, and
        # held the 75th percentile up on static alone.
        self._snr.append(1.0 if click else e / fl if fl > 0 else 0.0)
        self._back.append((e, s, fl, click))
        was = self._open
        on = 10 ** (self.snr_db / 20)
        if self._stat >= on and s > self._squelch * fl:
            self._open = True
        elif self._stat < on * 10 ** (-self.SQ_HYST_DB / 20) or s < 0.6 * self._squelch * fl:
            self._open = False
        if self._open and not was and not self._state and not self._code and not self._pending:
            # Just opened: the statistic needed a second or so of the
            # signal to rise, and the marks of that second were heard with
            # the squelch closed. Hear them again, open, out of the key-up
            # run they were folded into - or the first letters of every
            # over are lost. Against the signal's level now, not the level
            # each frame had then: before the signal rose, that was the
            # noise's own peak, and the noise ahead of the first letter was
            # heard as dots ("EIG5T" for ZL6B).
            k = min(len(self._back), self._run + 1)
            self._run = max(0, self._run + 1 - k)
            for fe, fs, ffl, fc in list(self._back)[-k:]:
                self._step(fe, max(fs, s), ffl, fc, learn=False)
            return
        self._step(e, s, fl, click, learn=True)

    def _step(self, e: float, s: float, fl: float, click: bool, learn: bool) -> None:
        """One frame through the mark/space state machine, with the squelch
        already decided; `learn` is False when frames are heard again."""
        span = s - fl
        thr = fl + (self.OFF if self._state else self.ON) * span
        mark = self._open and e > thr and not click
        self._closed = 0 if self._open else self._closed + 1
        if self._locked and self._closed * self.FRAME_S > self.REACQUIRE_S and not self._code:
            # A different station may come next, at its own speed.
            self._locked, self._marks, self._acq = False, [], []
        if learn and not self._state and not mark:
            # Learnt from key-up frames as their running median (each frame
            # nudges the ratio 0.3 % up or down): the ratio is the filter's
            # shape, which changes only when the filter does, and a median
            # is not dragged far by the faded marks of a QSB trough. A mean
            # of only the quieter frames (the first try) was biased low,
            # noise then cleared the squelch, and the squelch being open
            # kept the bias.
            # Starts at 1 (noise through a flat filter is as strong beside
            # the tone as on it) and moves 1 % a frame for its first two
            # seconds of key-up, to find a narrow filter's skirts quickly.
            # Not started from the first block's levels: a transmission
            # already under way when the decoder starts made that block
            # tone, not noise, and the floor sat on the tone.
            # The 30th percentile, not the median (steps up 0.6 and down 1.4
            # of the size, so they balance where 30 % of frames are under
            # it), scaled to the median by RATIO_C. While the squelch is
            # closed every frame is "key-up", a weak signal's marks
            # included, and they pulled a median up to the tone: on the
            # bench at 0 dB the ratio rose from 1 to 2.8 in two seconds,
            # the floor sat on the marks and the squelch never opened. Under
            # a signal up half the time the 30th percentile moves a quarter
            # as far.
            step = np.log(1.01 if self._learnt < 400 else 1.003)
            self._learnt += 1
            up = e > self._ratio * self._ref
            self._ratio *= float(np.exp(step * (2 * self.RATIO_Q if up else -2 * (1 - self.RATIO_Q))))
        if mark == self._state:
            self._pending = 0
            self._run += 1
            if self._state == self._inverted:      # a gap, read the right way round
                self._check_gap(self._run * self.FRAME_S)
            elif not self._in_carrier and self._run * self.FRAME_S > (
                    self.CARRIER_DITS * self._dot if self._locked else self.ACQ_CARRIER_S):
                # A carrier: finish the letter before it now, not when it ends.
                if not self._locked:
                    self._acquire(force=True)
                self._in_carrier = True
                self._flush_letter()
            return
        self._pending += 1
        # Debounce, 0.3 of a dot: a flicker shorter than that is noise,
        # folded back into the run in progress.
        need = int(max(2, round(0.3 * self._dot / self.FRAME_S)))
        if self._pending < need:
            self._run += 1
            if self._state == self._inverted:
                self._check_gap(self._run * self.FRAME_S)
            return
        dur = (self._run - (self._pending - 1)) * self.FRAME_S
        self._on_run(self._state, dur)
        self._state = mark
        self._run = self._pending
        self._pending = 0
        self._letter_done = self._space_done = False

    # --------------------------------------------------------------- timing
    def _letter_gap(self) -> float:
        g = 0.5 * (self._intra + self._inter)
        return min(max(g, 1.6 * self._dot), 2.6 * self._dot)

    def _word_gap(self) -> float:
        g = 0.5 * (self._inter + self._wordgap)
        return min(max(g, 4.0 * self._dot), 6.0 * self._dot)

    def _check_gap(self, dur: float) -> None:
        if not self._locked:
            # Still learning the speed: nothing is shown yet. A silence
            # longer than any word space (four of the longest mark so far:
            # twelve dots if it is a dash) ends the learning with what
            # there is, so a short over - a lone <SK> - is still shown.
            if self._marks and dur > max(0.4, 4 * max(self._marks)):
                self._acquire(force=True)
            else:
                return
        if not self._letter_done and dur > self._letter_gap():
            self._flush_letter()
            self._letter_done = True
        if not self._space_done and dur > self._word_gap():
            if self._text and not self._text.endswith(" "):
                self._emit(" ")
            self._space_done = True

    @staticmethod
    def _clusters(marks: list[float]) -> tuple[float, float] | None:
        """(dot, dash) from mark lengths: two-means on log length, the
        clusters' medians (a stray noise mark moves a median far less than
        a mean), believed only when each holds a seventh of the marks (at
        least two) and they are a plausible 2-4.5x apart. A run of only
        dots, or only dashes, is one cluster: None, and the caller keeps
        what it had."""
        x = np.log(np.array(marks))
        # Started from the data, not from the current estimate: started
        # from an estimate that had gone wrong (a noise flicker taken for
        # the dot), every real mark fell on one side, there was "one
        # cluster", and the wrong estimate was kept for good.
        cut = 0.5 * (x.min() + x.max())
        need = max(2, len(x) // 7)
        for _ in range(8):
            lo, hi = x[x <= cut], x[x > cut]
            if len(lo) < need or len(hi) < need:
                return None
            nxt = 0.5 * (lo.mean() + hi.mean())
            if nxt == cut:
                break
            cut = nxt
        d0, d1 = float(np.exp(np.median(lo))), float(np.exp(np.median(hi)))
        return (d0, d1) if 2.0 <= d1 / d0 <= 4.5 else None

    def _acquire(self, force: bool = False) -> None:
        """Lock the speed once the first marks make it plain, then decode
        everything heard so far with it. Deciding letters while the speed
        was still a guess (20 WPM, for a 12 WPM beacon) garbled the first
        word of every over, and the guess's errors then pulled the
        estimate the wrong way."""
        marks = [m for m in self._marks if m >= 0.5 * float(np.median(self._marks))] if self._marks else []
        # Flickers as the squelch opens, under half the typical mark, are
        # left out: two of them among the first six marks were taken for
        # a cluster of dots, and a 12 WPM beacon was read at 30.
        got = self._clusters(marks) if len(marks) >= self.ACQ_MARKS else None
        spaces = [d for m, d in self._acq if not m and d < self.ACQ_CARRIER_S]
        if got is not None:
            dot = 0.5 * (got[0] + got[1] / 3)
            # Cross-checked against the gaps: most gaps inside letters are
            # one dot long, so the gaps shorter than two dots should be.
            short = [d for d in spaces if d < 2 * dot]
            if len(short) < 2 or not 0.7 <= float(np.median(short)) / dot <= 1.4:
                got = None
        if got is None and not force and len(self._marks) < 3 * self.ACQ_MARKS:
            return
        if got is None and len(self._marks) < self.ACQ_MIN:
            # Forced (a long silence or a carrier after it) with fewer
            # marks than any message has: a flicker as the squelch opened,
            # the tail of the last over. Locking on it printed an "E" and
            # showed the guessed speed (an NCDXF slot opening with "E OH2B").
            # Forget it and learn from what comes next.
            self._marks, self._acq = [], []
            return
        if got is not None:
            self._dot = 0.5 * (got[0] + got[1] / 3)
            self._dash = got[1]
        elif len(spaces) >= 3:
            # No clusters (a few letters of only dots, or only dashes):
            # the lower half of the gaps is mostly gaps inside letters.
            sp = np.sort(spaces)
            self._dot = float(np.median(sp[:max(1, len(sp) // 2)]))
            self._dash = 3 * self._dot
        # The runs heard while learning are judged for polarity before any
        # is shown: by the time the speed is known the inside-out beacon
        # has already given two of its 7-dot "marks", and without this its
        # first line was printed as E/T soup before _polarity caught up.
        turned = False
        for was_mark, dur in (self._acq if self._polarity_on else ()):
            if self._turn(was_mark != self._inverted, dur):
                self._inverted, turned = not self._inverted, not turned
        if turned and not self._turning:
            # Learnt again the other way round - once: each pass hears the
            # same runs again in _turn, and runs that looked inside out
            # both ways (a neighbour's keying 200 Hz off, 20 dB up) turned
            # it over and back until Python's recursion limit.
            self._acq = [(not m, d) for m, d in self._acq]
            self._marks = [d for m, d in self._acq if m and d < self.ACQ_CARRIER_S][-self.HISTORY:]
            self._dot, self._dash = 1.2 / self._wpm_hint, 3.6 / self._wpm_hint
            self._turning = True
            try:
                self._acquire(force)
            finally:
                self._turning = False
            return
        self._marks = []   # the replay below hears every mark again
        self._intra, self._inter, self._wordgap = self._dot, 3 * self._dot, 7 * self._dot
        self._locked = True
        runs, self._acq = self._acq, []
        for was_mark, dur in runs:
            self._letter_done = self._space_done = False   # each replayed gap is a new one
            self._apply(was_mark, dur)
        self._letter_done = self._space_done = False

    def _polarity(self, tone: bool, dur: float) -> None:
        """Read the keying inside out, or back the right way, when the long
        runs say so (_turn); learnt afresh from the run that decided it."""
        if not self._polarity_on or not self._locked or not self._turn(tone, dur):
            return
        self._inverted = not self._inverted
        self._locked, self._marks, self._acq = False, [], []
        self._code, self._gaps = "", []
        self._in_carrier = False

    def _turn(self, tone: bool, dur: float) -> bool:
        """Whether the long runs, with this one, say the keying is being
        read the wrong way round. `tone` is whether the run was tone.

        Counted only once the speed is known (a 12 WPM dash is five dots of
        the 20 WPM guess), over the last LONG_HISTORY long runs, so it
        survives the squelch closing in the beacon's key-down and the
        speed being learnt afresh after it. Two long tones and three times
        as many as long gaps turn it over; the same the other way turns it
        back. A long tone from real keying needs two dashes run together,
        so one fade does not do it, and the word spaces of the same signal
        outvote it. Twice as many was not enough: light keying at 6 dB
        (bench cq20-light), its dashes run together in the noise, gave
        four long tones to two word spaces, and the rest of the over was
        read inside out ("IO75R ITIAEITIAEITIMEAE5T"); keying really
        inside out has no long gaps at all."""
        lo, hi = self.LONG_DOTS
        if not lo * self._dot < dur < hi * self._dot:
            return False
        self._long = (self._long + [tone])[-self.LONG_HISTORY:]
        tones = sum(self._long)
        gaps = len(self._long) - tones
        if not self._inverted:
            return tones >= 2 and tones >= 3 * gaps
        return gaps >= 2 and gaps >= 3 * tones

    def _on_run(self, was_mark: bool, dur: float) -> None:
        if dur <= 0:
            return
        self._polarity(was_mark, dur)
        was_mark = was_mark != self._inverted
        if self._locked:
            self._apply(was_mark, dur)
            return
        self._acq.append((was_mark, dur))
        if was_mark and dur < self.ACQ_CARRIER_S:
            self._marks = (self._marks + [dur])[-self.HISTORY:]
            self._acquire()

    def _apply(self, was_mark: bool, dur: float) -> None:
        if was_mark:
            if self._in_carrier or dur > self.CARRIER_DITS * self._dot:
                self._in_carrier = False
                self._code, self._gaps = "", []
                return
            if dur < 0.45 * self._dot:
                return   # a noise flicker that outlasted the debounce
            is_dash = dur > 0.5 * (self._dot + self._dash)
            if self._code:
                self._gaps.append(self._last_gap)
            self._code += "-" if is_dash else "."
            # The single-class estimate moves too, so a speed change is
            # followed even while the clusters are lopsided; clipped, so
            # one noise-cut element cannot drag the speed far.
            if is_dash:
                self._dash += 0.2 * (min(max(dur, 0.6 * self._dash), 1.6 * self._dash) - self._dash)
            else:
                self._dot += 0.2 * (min(max(dur, 0.6 * self._dot), 1.6 * self._dot) - self._dot)
            self._marks = (self._marks + [dur])[-self.HISTORY:]
            got = self._clusters(self._marks)
            if got is not None:
                dot, dash = got
                self._dot += 0.3 * (0.5 * (dot + dash / 3) - self._dot)
                self._dash += 0.3 * (dash - self._dash)
            self._dash = min(max(self._dash, 2.0 * self._dot), 4.5 * self._dot)
            return
        self._last_gap = dur
        if dur <= self._letter_gap():
            self._intra += 0.2 * (min(dur, 2 * self._dot) - self._intra)
        elif dur <= self._word_gap():
            self._inter += 0.2 * (dur - self._inter)
        elif dur < 12 * self._dot:
            self._wordgap += 0.2 * (dur - self._wordgap)
        self._intra = min(max(self._intra, 0.6 * self._dot), 1.6 * self._dot)
        self._inter = min(max(self._inter, 2.0 * self._dot), 4.5 * self._dot)
        self._wordgap = min(max(self._wordgap, 5.0 * self._dot), 10.0 * self._dot)
        self._check_gap(dur)

    # --------------------------------------------------------------- output
    @staticmethod
    def _lookup(code: str) -> str | None:
        return CODE_TO_PROSIGN.get(code) or CODE_TO_CHAR.get(code)

    def _split(self, code: str, gaps: list[float]) -> str | None:
        """`code` as two letters, split at its longest gap, or None."""
        if len(code) < 2 or len(gaps) != len(code) - 1:
            return None
        for i in np.argsort(gaps)[::-1][:2]:
            a, b = self._lookup(code[:i + 1]), self._lookup(code[i + 1:])
            if a and b:
                return a + b
        return None

    def _flush_letter(self) -> None:
        if not self._code:
            return
        ch = self._lookup(self._code) or self._split(self._code, self._gaps) or "*"
        self._code, self._gaps = "", []
        self._emit(ch)

    def _emit(self, ch: str) -> None:
        self._text += ch
        # NB's event shape (openhamshack-nb docs/SERVER-API.md, the `events`
        # feed), which every client's CW strip already reads: one event per
        # character, a space with no speed.
        if ch == " ":
            ev = {"mode": "cw", "text": " ", "freq": self.freq}
        else:
            ev = {"mode": "cw", "text": ch, "freq": self.freq, "wpm": round(1.2 / self._dot, 1)}
        self._out.append(ev)
        if self.emit is not None:
            self.emit(ev)

    @property
    def text(self) -> str:
        """Everything decoded since the last reset."""
        return self._text

    @property
    def rate(self) -> float | None:
        """The sample rate of the last feed() (None before the first)."""
        return self._rate

    @property
    def wpm(self) -> float | None:
        """The sender's speed once it has been learnt, else None."""
        return round(1.2 / self._dot, 1) if self._locked else None

    @property
    def pitch(self) -> float:
        """Where the tone is believed to be, in Hz of audio: the pitch it
        was given, moved by AFC onto the tone it has found."""
        return float(self._f)

    @property
    def locked(self) -> bool:
        """The speed is learnt and letters are being printed."""
        return self._locked

    @property
    def snr(self) -> float | None:
        """The tone over the noise beside it, in dB in the decoder's own
        bandwidth, as the squelch judges it (the 75th percentile over the
        last 2.5 s); None until there is a window of it."""
        return round(20 * float(np.log10(self._stat)), 1) if self._stat > 0 else None

    @property
    def hearing(self) -> bool:
        """A signal stands over the noise (the squelch is open)."""
        return self._open
