"""Callsigns and spots from the skimmer's channels, the way CW Skimmer and
the Reverse Beacon Network make them (CW-SKIMMER-PLAN §3).

Pure Python on text and timestamps: no numpy, nothing that can key, and
nothing of an app's - the country lookup (cty.dat) and the call history
are handed in as callables, so shack_cw stays free of ohscore. An app
passes its own loader (openhamshack-nb ohscore/dxcc.py, the operator's file
first), the one its decodes already name countries with.

Extraction, on each channel's words (`Calls.word`):

* `CQ [CQ...] [TEST|DX|POTA|...] <CALL>`, `<CALL> TEST`, `TEST <CALL>`:
  the call is running here, kind `cq`.
* `DE <CALL>`: transmitting here, `dx` (`cq` if a CQ came just before).
* `TU <CALL>`, `<CALL> <CALL>`: the same station identifying, `dx`.
* `VVV DE <CALL>`, `<CALL>/B`, or any call on an NCDXF/IARU beacon
  frequency: `beacon`.
* `<OTHER> DE <ME>`: OTHER is being worked from elsewhere; never a spot
  here. That is why a word is judged only once words after it are in
  (LOOKAHEAD; or the channel has gone quiet: `flush`).

Before any of that, the printed words are spaced again (`segment`): the
decoders' word spaces are the least reliable thing they print, and a call
run onto its CQ or split by its sender's letter gaps matches no pattern.

Validation, before anything is a spot:

1. Shape: an ITU-shaped call (a 1-2 character prefix with its digit, a
   suffix ending in a letter, an optional /P-style part either side), not
   a locator or a word of the stop list, and - if a country lookup is
   given - in a DXCC entity (CW Skimmer's prefix-block test).
2. The spot rule (Brian, 14/10/2026; the constants below the imports):
   a call is confirmed only when it passes the callsign check AND has
   been decoded SEEN times within HIT_HZ in HIT_S, by either engine when
   the skimmer runs both. The callsign check is the history - a callsign
   list (scp.py: the RBN week shipped with the language model, and
   MASTER.SCP if the operator has it), the logbook, calls already
   confirmed this session - and, for a call the list does not hold or when there is no
   list, the shape and DXCC test of 1 with UNLISTED_SEEN decodes.
   Anything less is `unconfirmed`: listed dim, never sent outward.
3. Busted: one Morse-similar edit from a call confirmed within HIT_HZ in
   HIT_S with more hits (E/I/S/H/5 for dits, T/M/O/0 for dahs, an element
   dropped or added at either end of one letter...). Kept, pointing at the
   winner, never spotted.
4. Confidence 0-1 for sorting and dimming; `qsy` when a confirmed call is
   now more than QSY_HZ from where it was spotted.

A spot is sent again (same id) when its status, kind or frequency changes,
or its hits do up to HITS_SAT; otherwise at most every RESPOT_S.
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from ..morse import MORSE

# ---- The spot rule, in one place (Brian, 14/10/2026) -----------------
# A call is spotted only after it passes the callsign check and has been
# seen enough times: decoded that often, by either engine, on the same
# signal (within HIT_HZ) within HIT_S. HIT_HZ is the tracker's merge
# distance with room for drift; HIT_S covers a CQ-and-listen station's
# next over (quiet 20 s or more between them) and a QSO's exchange.
#
# The numbers were chosen on 57 RBN-labelled recordings (6.6 h: ohs-box,
# Flex and KiwiSDR; never the decoder bench or the network's dev set),
# replaying every hit through this class (ohs-libs
# python/shack_cw/tools/skim_eval.py; office ~/build/cw-dual-eval). RBN
# calls spotted / valid spots / share of them the RBN spotted that day,
# both engines with the language model in the network's search:
#   classic engine, seen twice (0.1.21-beta.1)        52 / 114 / 73 %
#   both, seen twice                                   75 / 200 / 68 %
#   both, shape only, seen three times                 61 / 121 / 77 %
#   both, listed twice, unlisted three times:
#     list = the RBN week shipped in nn/cwlm.npz       70 / 157 / 77 %
#     list = MASTER.SCP                                66 / 142 / 76 %
#     list = both                                      72 / 166 / 77 %
#   both, listed calls only (the RBN week)             62 / 113 / 89 %
# Half the RBN-confirmed calls we spot are on no list, so a list confirms
# a call but never gates one; a call it does not vouch for - or any call
# while there is no list - must be seen once more instead. Four decodes
# for an unlisted call scored a little better still, but the score
# favours any list made from the RBN by construction, so three stands.
SEEN = 2             # decodes of a call the list holds (or confirmed this session)
UNLISTED_SEEN = 3    # ... of any other call; None: never spotted
HIT_HZ = 150.0
HIT_S = 600.0
# Both engines printing the same call in one over is two decodes: two
# different readers agreeing on a whole call is as good evidence as one
# reader printing it twice. Counting such pairs once (AGREE_S = 10) cost
# 4-9 calls of the rows above for no gain in precision; 0 counts every hit.
AGREE_S = 0.0
QSY_HZ = 1000.0
RESPOT_S = 600.0
KEEP_S = 1800.0
KEEP_N = 500
HITS_SAT = 4
NCDXF_HZ = (14100000.0, 18110000.0, 21150000.0, 24930000.0, 28200000.0)
NCDXF_WITHIN = 500.0

# A home call as the ITU allocates them: a prefix of one or two letters,
# a letter and a digit, or a digit and a letter; then the call-area digit;
# then a suffix of up to four, ending in a letter. Stricter than
# ohscore/callsign.py's typing check on purpose: that one admits GB100RSGB
# for an operator typing their own call, where a skimmer reading noise
# needs every reason to say no.
_HOME = re.compile(r"^(?:[A-Z]{1,2}|[A-Z][0-9]|[0-9][A-Z])[0-9][A-Z0-9]{0,3}[A-Z]$")
_PART = re.compile(r"^[A-Z0-9]{1,4}$")
_LOCATOR = re.compile(r"^[A-R]{2}[0-9]{2}[A-X]{2}$")
STOP = frozenset({"CQ", "DE", "TU", "TEST", "QRZ", "QSL", "QSO", "QRL", "5NN", "599", "559", "ENN", "73", "88",
                  "RST", "UR", "NAME", "QTH", "OP", "HR", "BK", "KN", "AR", "SK", "K", "R", "GM", "GA", "GE",
                  "TNX", "FB", "PSE", "AGN", "VVV", "NR", "WX", "ANT", "PWR", "RIG", "HW", "CPY", "SRI", "DR",
                  "OM", "ES", "FER", "UP", "QRS", "QRQ", "POTA", "SOTA", "WWFF", "IOTA"})
CQ_MODS = frozenset({"TEST", "DX", "POTA", "SOTA", "WWFF", "IOTA", "NA", "EU", "AS", "AF", "OC", "SA", "JA", "FD",
                     "SST", "MST", "CWT", "QRP", "BK", "CQ"})
SIGNOFF = frozenset({"K", "KN", "AR", "BK", "<AR>", "<KN>", "<BK>", "PSE", "TEST"})

# Letters that sound alike (CW-SKIMMER-PLAN §3.2): a busted copy swaps one
# for another, or gains or loses an element at one end of one letter.
_ALIKE = ["EISH5", "TMO0", "AWJ1", "NDB6", "UV4"]


def shape_ok(call: str) -> bool:
    """An ITU-shaped call, perhaps with one /part either side (EA8/G4ABC,
    G4ABC/P, OH2B/B); not a locator, not a word of the stop list."""
    if not call or call in STOP or "*" in call or len(call) > 14:
        return False
    bits = call.split("/")
    if len(bits) > 3 or not all(bits):
        return False
    homes = [b for b in bits if _HOME.match(b) and not _LOCATOR.match(b)]
    if len(homes) != 1:
        return False
    return all(b in homes or _PART.match(b) for b in bits)


def home_of(call: str) -> str:
    for b in call.split("/"):
        if _HOME.match(b):
            return b
    return call


def _alike(a: str, b: str) -> bool:
    """Whether letter b is a Morse-similar misreading of letter a."""
    if a == b:
        return False
    if any(a in g and b in g for g in _ALIKE):
        return True
    ca, cb = MORSE.get(a), MORSE.get(b)
    if not ca or not cb:
        return False
    return cb in (ca[1:], ca[:-1]) or ca in (cb[1:], cb[:-1])


def similar(a: str, b: str) -> bool:
    """One Morse-similar edit apart: one letter misread, or an E or a T
    (a single element) gained or lost."""
    if a == b:
        return False
    if len(a) == len(b):
        diff = [(x, y) for x, y in zip(a, b) if x != y]
        return len(diff) == 1 and _alike(*diff[0])
    if abs(len(a) - len(b)) == 1:
        s, l = (a, b) if len(a) < len(b) else (b, a)
        for i in range(len(l)):
            if l[i] in "ET" and l[:i] + l[i + 1:] == s:
                return True
    return False


_PROSIGN = re.compile(r"(<[A-Z]{2}>)")
# Words a channel prints that are worth splitting a run of letters for:
# the stop list's and CQ's words of two letters or more (a lone K or R is
# as likely the tail of a call as a sign-off).
_KEYWORDS = frozenset(w for w in STOP | CQ_MODS if len(w) >= 2)

# The segmenter's scores (segment): what a token is worth, and what each
# change to the printed spacing costs. Tuned on the box's 20 m recording
# and the Kiwi corpus (shack_cw.skim.recovery), and pinned by the tests'
# cases from them: a split must buy a keyword before a call or another
# keyword, or a call; a merge must buy a call. A keyword alone is worth
# nothing: worth something, the DE that ends many a call was taken off it
# ("CQCQCQI Z 5 F D E", IZ5FDE on the box as an experimental decoder
# printed it, read IZ5F DE).
CALL_SCORE = 3.0
CALL_PER_CHAR = 0.05    # a longer call explains more of what was printed: ties go to it
KEYWORD_SCORE = 0.0
BEFORE_CALL = 1.0       # a keyword just before a call (DE SM5D, CQ GM4BRI)
BEFORE_KEYWORD = 1.0    # ... or before another (CQ DE, CQ CQ)
REPEATED = 1.5          # a call just after itself (IZ5FDE IZ5FDE)
SPLIT_COST = 0.8        # a word space added where the decoder printed none
MERGE_COST = 1.0        # a word space the decoder printed, taken out
MERGE_SHORT = 0.4       # ... beside a one-letter word
MAX_CALL = 14
LOOKAHEAD = 5           # words after a word before it is judged (Calls.word)
KEEP_WORDS = 8          # judged words kept per channel, for classify's four before
MAX_RAW = 12            # printed words held unjudged at most (a long run of junk)


def segment(words: list[str], is_call: Callable[[str], bool] = shape_ok) -> list[str]:
    """Printed words as the matcher's tokens: the spacing that makes the
    most calls and keywords for the fewest changes to what was printed.

    A decoder's word spaces are the weakest thing it prints. Real fists
    shorten the space after CQ and DE until it is a letter gap ("CQDESM5D",
    "TIQCQDESM5D": SM5D on the box's recording, every over), and lengthen
    letter gaps until they are word spaces ("I Z 5FDE", "DE OE 7AJI"). A
    pattern per fault (the split_glued and _mend of the first version) fixed
    the cases it was written for. This instead tries every spacing of the
    run of letters - each printed space kept or taken out, a space added
    anywhere a call or a keyword could start or end - and keeps the best
    score: CALL_SCORE a call, KEYWORD_SCORE a keyword, more for a keyword
    before a call or another keyword, less SPLIT_COST for each space added
    and MERGE_COST for each taken out. `is_call` is the shape test, and the
    country lookup when there is one (Calls passes both), so a merge or a
    split is only ever made for a call in a DXCC entity.

    Prosigns stand apart and are never joined to anything ("<BT>DEAA9H"
    is "<BT> DE AA9H"). A dynamic programme over the cut points, which are
    the printed spaces and the ends of every possible call or keyword: a
    few dozen per word, so it costs a channel little."""
    out: list[str] = []
    run: list[str] = []
    for w in words:
        for piece in _PROSIGN.split(w):
            if not piece:
                continue
            if piece.startswith("<"):
                out += _segment_run(run, is_call)
                out.append(piece)
                run = []
            else:
                run.append(piece)
    return out + _segment_run(run, is_call)


def _segment_run(words: list[str], is_call) -> list[str]:
    if len(words) <= 1 and (not words or len(words[0]) < 4):
        return list(words)
    s = "".join(words)
    n = len(s)
    # Each printed space, and what taking it out costs: less beside a
    # word of one letter, and nothing between two, since a run of them
    # ("U A 6 H N U", UA6HNU on the box) is a sender's letter gaps read as
    # word spaces, not words. A keyword as printed is never joined to its
    # neighbour ("TU 5NN" is no call TU5NN), nor is a lone K, the over's
    # last word ("U A 6 H N U K" is UA6HNU, K). A call as printed is never
    # split - "IZ5FDE IZ5FDE" is no "IZ5F DE IZ5FDE" (a call ending in DE,
    # and DE before a call scores) - and is joined to a neighbour only at
    # the full MERGE_COST, which only context repays: "G4ABC E" stays, but
    # "DE L A9OI L A9OI" (LA9OI on the box, its letter gaps of up to 4.8
    # dots read as spaces) is DE and the call twice.
    kept = [w in _KEYWORDS or w == "K" for w in words]
    whole = [k or (len(w) >= 3 and is_call(w)) for w, k in zip(words, kept)]
    spaces, p = {}, 0
    for k, (w, nxt) in enumerate(zip(words, words[1:])):
        p += len(w)
        short = (len(w) == 1) + (len(nxt) == 1)
        if kept[k] or kept[k + 1]:
            spaces[p] = float("inf")
        elif whole[k] or whole[k + 1]:
            spaces[p] = MERGE_COST
        else:
            spaces[p] = (MERGE_COST, MERGE_SHORT, 0.0)[short]
    # Candidate tokens other than plain junk: (start, end, kind).
    typed: dict[tuple[int, int], int] = {}
    for i in range(n):
        for j in range(i + 2, min(n, i + MAX_CALL) + 1):
            t = s[i:j]
            if j - i <= 4 and t in _KEYWORDS:
                typed[(i, j)] = 1
            elif j - i >= 3 and any(ch.isdigit() for ch in t) and is_call(t):
                typed[(i, j)] = 2
    inside_whole, p = set(), 0
    for w, wh in zip(words, whole):
        if wh:
            inside_whole.update(range(p + 1, p + len(w)))
        p += len(w)
    cuts = sorted(({0, n} | set(spaces) | {a for a, _ in typed} | {b for _, b in typed}) - inside_whole)
    # best[cut][state of the token ending there] = (score, previous cut,
    # previous state); a state is (kind, the call if it is one), so a call
    # sent twice can score as such.
    Key = tuple
    best: dict[int, dict[Key, tuple[float, int, Key]]] = {0: {(0, ""): (0.0, -1, (0, ""))}}
    for bi, j in enumerate(cuts[1:], 1):
        here: dict[Key, tuple[float, int, Key]] = {}
        edge = 0.0 if j == n or j in spaces else -SPLIT_COST
        for i in cuts[:bi]:
            if i not in best:
                continue
            kind = typed.get((i, j), 0)
            inside = [c for q, c in spaces.items() if i < q < j]
            if kind == 0 and inside:
                continue        # junk is never joined to junk
            score = CALL_SCORE + CALL_PER_CHAR * (j - i) if kind == 2 else KEYWORD_SCORE if kind == 1 else 0.0
            gain = score - sum(inside) + edge
            key = (kind, s[i:j] if kind == 2 else "")
            for pk, (sc, _, _) in best[i].items():
                adj = 0.0
                if pk[0] == 1:
                    adj = BEFORE_CALL if kind == 2 else BEFORE_KEYWORD if kind == 1 else 0.0
                elif kind == 2 and pk == key:
                    adj = REPEATED
                v = sc + gain + adj
                if key not in here or v > here[key][0] + 1e-9:
                    here[key] = (v, i, pk)
        if here:
            best[j] = here
    # Back from the end; ties go to the printed spacing (the junk path).
    k = max(best[n], key=lambda kk: (round(best[n][kk][0], 9), kk[0] == 0))
    toks, j = [], n
    while j > 0:
        _, i, pk = best[j][k]
        toks.append(s[i:j])
        j, k = i, pk
    return toks[::-1]


def tokens(word: str) -> list[str]:
    """One printed word as the matcher's tokens (segment, shape only)."""
    return segment([word])


def classify(words: list[str], i: int) -> str | None:
    """The kind of spot word i makes, from the words round it: "cq", "dx",
    "beacon", "worked" (a call being worked from elsewhere: never a spot
    here), or None (no reason to think it is a call on this frequency)."""
    w = words[i]
    if not shape_ok(w):
        return None
    prev = words[i - 1] if i > 0 else ""
    nxt = words[i + 1] if i + 1 < len(words) else ""
    if nxt == "DE" or (nxt == w and i + 2 < len(words) and words[i + 2] == "DE"):
        return "worked"
    if w.endswith("/B"):
        return "beacon"
    before = words[max(0, i - 4):i]
    if prev == "DE":
        if "VVV" in before:
            return "beacon"
        return "cq" if "CQ" in before else "dx"
    j = i - 1
    while j >= 0 and (words[j] in CQ_MODS or words[j] == w):
        if words[j] == "CQ":
            return "cq"
        j -= 1
    if prev == "TEST" or nxt == "TEST":
        return "cq"
    if prev == "TU":
        return "dx"
    if nxt == w or prev == w:
        return "dx"
    if nxt in SIGNOFF and prev in ("", "DE") and i == 0:
        return "dx"
    return None


@dataclass
class Spot:
    id: int
    call: str
    f: float
    track: int
    first: float
    last: float
    kind: str
    hits: int = 1
    status: str = "unconfirmed"
    wpm: list = field(default_factory=list)
    snr: float | None = None
    band: str | None = None
    country: dict | None = None
    busted_of: str | None = None
    spot_f: float | None = None     # where it was first confirmed (for qsy)
    sent: tuple | None = None       # what was last sent: (status, kind, hits, f)
    sent_t: float = -1e18
    in_history: bool = False
    moved: bool = False
    engines: set = field(default_factory=set)   # which engines printed it


class Calls:
    """Turns channels' words into spots (see the module).

    `country(call)` -> {"country", "dxcc", "continent"} or None for a call
    in no DXCC entity; None (not given) checks the shape only.
    `history(call)` -> True when the call is in the callsign list or the
    logbook, False when it is not, None when there is no list to ask
    (scp.Scp before its file is in); not given is the same as None."""

    def __init__(self, country: Callable[[str], dict | None] | None = None,
                 history: Callable[[str], bool] | None = None, pitch: float = 700.0):
        self.country = country
        self.history = history
        self.pitch = pitch
        self.spots: dict[int, Spot] = {}
        self.rev = 0
        self._next = 1
        # Per stream: a track, or (track, engine) when a skimmer runs
        # both engines, each engine's words judged apart.
        self._words: dict[object, deque] = {}
        self._raw: dict[object, list] = {}      # [printed word, (f, t, wpm, snr, band)] not yet judged
        self._hits: deque = deque()            # (t, call, f, engine)
        self._confirmed: set[str] = set()       # calls confirmed this session
        self._country_cache: dict[str, dict | None] = {}

    # ----------------------------------------------------------- the words
    def word(self, track: int, word: str, f: float, t: float, wpm: float | None = None,
             snr: float | None = None, band: str | None = None, engine: str | None = None) -> list[dict]:
        """A word completed on `track` at `f` Hz; the spots it changed,
        each as cwspot's `spot` (with `rev`).

        The words as printed are kept until judged, and every new word
        spaces all of them again (segment): a call split by letter gaps
        read as word spaces is only whole once its last part is in ("I Z
        5FDE", or "U A 6 H N U", six words), and a part merged too early
        (UA6HN, before the last U) is undone. A token is judged once
        LOOKAHEAD tokens follow it: two so "<OTHER> DE <ME>" is known, the
        rest for segment. `engine`: which engine printed it, when a skimmer
        runs more than one; each engine's words are a stream of their own."""
        stream = track if engine is None else (track, engine)
        self._raw.setdefault(stream, []).append([word, (f, t, wpm, snr, band)])
        return self._advance(stream, False)

    def _advance(self, stream, final) -> list[dict]:
        raw = self._raw.get(stream) or []
        if not raw:
            return []
        track, engine = stream if isinstance(stream, tuple) else (stream, None)
        toks = segment([w for w, _ in raw], self._is_call)
        hist = self._words.setdefault(stream, deque(maxlen=KEEP_WORDS))
        n = len(toks) if final else max(len(toks) - LOOKAHEAD, 1 if len(raw) > MAX_RAW else 0)
        if n <= 0:
            return []
        words = list(hist) + toks
        out: list[dict] = []
        # Each token is judged with the frequency, time, speed and SNR of
        # the printed word it ends in: the call as it was heard, not as the
        # channel was LOOKAHEAD words later.
        ends, c = [], 0
        for w, meta in raw:
            c += len(w)
            ends.append((c, meta))
        c = 0
        for k in range(n):
            c += len(toks[k])
            meta = next(m for e, m in ends if e >= c)
            out += self._judge(track, words, len(hist) + k, *meta, engine=engine)
        hist.extend(toks[:n])
        # The printed words those tokens came from are done; a word split
        # across the line keeps its unjudged end.
        while raw and len(raw[0][0]) <= c:
            c -= len(raw.pop(0)[0])
        if raw and c:
            raw[0][0] = raw[0][0][c:]
        return out

    def _is_call(self, s: str) -> bool:
        """A call's shape, in a DXCC entity when there is a lookup."""
        return shape_ok(s) and (self.country is None or self._country(s) is not None)

    def flush(self, track: int, f: float, t: float, wpm: float | None = None, snr: float | None = None,
              band: str | None = None, engine: str | None = None) -> list[dict]:
        """The channel went quiet: judge the words still waiting for the
        ones after them (each with what was known when it was printed;
        the arguments are the same as word()'s, for the caller's ease)."""
        return self._advance(track if engine is None else (track, engine), True)

    def forget(self, track: int) -> None:
        """A track is gone: every stream of it."""
        for d in (self._words, self._raw):
            for k in [k for k in d if k == track or (isinstance(k, tuple) and k[0] == track)]:
                del d[k]

    def _judge(self, track, words, i, f, t, wpm, snr, band, engine=None) -> list[dict]:
        kind = classify(words, i)
        if kind is None or kind == "worked":
            return []
        call = words[i]
        if any(abs(f - b) <= NCDXF_WITHIN for b in NCDXF_HZ):
            kind = "beacon"
        info = self._country(call)
        if self.country is not None and info is None:
            return []
        return self._hit(call, kind, track, f, t, wpm, snr, band, info, engine)

    def _country(self, call: str) -> dict | None:
        if self.country is None:
            return None
        if call not in self._country_cache:
            try:
                self._country_cache[call] = self.country(call)
            except Exception:
                self._country_cache[call] = None   # a lookup that fails is no entity
        return self._country_cache[call]

    # ------------------------------------------------------------ the spots
    def _hit(self, call, kind, track, f, t, wpm, snr, band, info, engine=None) -> list[dict]:
        self._hits.append((t, call, f, engine))
        while self._hits and t - self._hits[0][0] > HIT_S:
            self._hits.popleft()
        sp = next((s for s in self.spots.values() if s.call == call and
                   (s.track == track or abs(s.f - f) <= QSY_HZ)), None)
        if sp is None:
            sp = next((s for s in self.spots.values() if s.call == call), None)
        if sp is None:
            sp = Spot(id=self._next, call=call, f=f, track=track, first=t, last=t, kind=kind, band=band,
                      country=info)
            self._next += 1
            self.spots[sp.id] = sp
        else:
            sp.hits += 1
            sp.last = t
            sp.track = track
            # A CQ seen within the window makes the station a runner; a
            # beacon stays a beacon.
            if sp.kind != "beacon" and (kind == "cq" or kind == "beacon"):
                sp.kind = kind
            sp.f = f
            sp.band = band or sp.band
        if engine is not None:
            sp.engines.add(engine)
        if wpm:
            sp.wpm = (sp.wpm + [float(wpm)])[-HITS_SAT * 2:]
        if snr is not None:
            sp.snr = float(snr)
        sp.in_history = self._listed(call) is True
        out = self._status(sp, t)
        # A call newly confirmed may bust neighbours already listed.
        if sp.status == "valid":
            for o in list(self.spots.values()):
                if o is not sp and o.status in ("valid", "unconfirmed") and abs(o.f - sp.f) <= HIT_HZ \
                        and similar(sp.call, o.call) and sp.hits > o.hits:
                    out += self._status(o, t)
        self._prune(t)
        return out

    def _listed(self, call: str):
        """The callsign list's answer for a call: True, False, or None when
        there is no list (or it cannot answer)."""
        if self.history is None:
            return None
        try:
            got = self.history(call)
        except Exception:
            return None
        return None if got is None else bool(got)

    def _seen(self, call: str, f: float) -> int:
        """How many times the call has been decoded on this signal in the
        window (the spot rule), by either engine; with AGREE_S, hits of
        different engines that close together count once."""
        hits = sorted((t, e) for t, c, ff, e in self._hits if c == call and abs(ff - f) <= HIT_HZ)
        if AGREE_S <= 0:
            return len(hits)
        n, last = 0, None
        for t, e in hits:
            if last is not None and e != last[1] and t - last[0] < AGREE_S:
                last = None          # the other engine's copy of the one just counted
                continue
            n += 1
            last = (t, e)
        return n

    def _needed(self, call: str) -> int | None:
        """Decodes the spot rule asks of this call: SEEN for a call in the
        list or confirmed this session, UNLISTED_SEEN for any other (one
        the list does not hold, or every call while there is no list)."""
        if call in self._confirmed or self._listed(call) is True:
            return SEEN
        return UNLISTED_SEEN

    def _status(self, sp: Spot, t: float) -> list[dict]:
        winner = None
        for o in self.spots.values():
            if o is not sp and o.status in ("valid", "qsy") and abs(o.f - sp.f) <= HIT_HZ \
                    and t - o.last <= HIT_S and similar(o.call, sp.call) and o.hits > sp.hits:
                winner = o
                break
        if winner is not None:
            sp.status, sp.busted_of = "busted", winner.call
        elif (need := self._needed(sp.call)) is not None and self._seen(sp.call, sp.f) >= need:
            sp.busted_of = None
            if sp.spot_f is None:
                sp.spot_f = sp.f
            if abs(sp.f - sp.spot_f) > QSY_HZ:
                sp.moved = True
                sp.spot_f = sp.f
            # Once moved it stays "qsy" at its new frequency: a client
            # that missed the item where it moved still sees why.
            sp.status = "qsy" if sp.moved else "valid"
            self._confirmed.add(sp.call)
        else:
            sp.status = "unconfirmed"
        key = (sp.status, sp.kind, min(sp.hits, HITS_SAT), round(sp.f, -1))
        changed = sp.sent is None or key[:2] != sp.sent[:2] or key[2] != sp.sent[2] or \
            abs(key[3] - sp.sent[3]) > QSY_HZ or t - sp.sent_t >= RESPOT_S
        if not changed:
            return []
        sp.sent, sp.sent_t = key, t
        self.rev += 1
        return [dict(self.as_dict(sp, t), rev=self.rev)]

    def confidence(self, sp: Spot) -> float:
        c = 0.4 * min(sp.hits, HITS_SAT) / HITS_SAT
        if sp.in_history:
            c += 0.15
        if len(sp.wpm) >= 2 and max(sp.wpm) <= 1.15 * min(sp.wpm):
            c += 0.15
        if sp.snr is not None and sp.snr > 10:
            c += 0.15
        if sp.kind == "cq":
            c += 0.15
        if sp.status in ("busted", "unconfirmed"):
            c *= 0.5
        return round(min(1.0, c), 2)

    def as_dict(self, sp: Spot, t: float | None = None) -> dict:
        wpm = sorted(sp.wpm)
        d = {"id": sp.id, "call": sp.call, "f": round(sp.f, -1), "dial": round(sp.f - self.pitch, -1),
             "mode": "cw", "wpm": wpm[len(wpm) // 2] if wpm else None,
             "snr": None if sp.snr is None else round(sp.snr, 1), "kind": sp.kind, "status": sp.status,
             "hits": sp.hits, "first": sp.first, "last": sp.last, "band": sp.band, "channel": sp.track,
             "confidence": self.confidence(sp)}
        info = sp.country or {}
        for k in ("country", "dxcc", "continent"):
            d[k] = info.get(k)
        if sp.busted_of:
            d["bustedOf"] = sp.busted_of
        return d

    def list(self, t: float, limit: int = KEEP_N) -> list[dict]:
        """Every spot of the last KEEP_S, newest first."""
        out = [self.as_dict(s, t) for s in self.spots.values() if t - s.last <= KEEP_S]
        out.sort(key=lambda d: -d["last"])
        return out[:limit]

    def call_on(self, track: int) -> tuple[str, str] | None:
        """(call, kind) of the newest confirmed spot on a track, if any."""
        best = None
        for s in self.spots.values():
            if s.track == track and s.status in ("valid", "qsy") and (best is None or s.last > best.last):
                best = s
        return (best.call, best.kind) if best else None

    def _prune(self, t: float) -> None:
        old = [k for k, s in self.spots.items() if t - s.last > KEEP_S]
        for k in old:
            del self.spots[k]
        if len(self.spots) > KEEP_N:
            for k, _ in sorted(self.spots.items(), key=lambda kv: kv[1].last)[:len(self.spots) - KEEP_N]:
                del self.spots[k]
