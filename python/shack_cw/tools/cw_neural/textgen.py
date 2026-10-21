"""What the synthetic stations send: the language of real CW overs.

The network learns letters from their shapes, but which letters follow
which is part of how an operator copies a weak call, and the bench's
calls sit inside CQs, contest exchanges and QSOs. So the text is drawn
from the overs the bands carry (CQ in its many forms, contest and POTA
exchanges, rag-chew, beacons) with callsigns built from the prefixes of
AD1C's cty.dat (the file shack_bandplan already ships), plus a share of
random letter groups and call lists, so the network cannot lean on the
language alone.

Nothing here is a real station's text; calls are random and only their
shapes are real.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import numpy as np

CTY = Path(__file__).resolve().parents[4] / "data" / "bandplan" / "cty" / "cty.dat"

# Entities that fill the bands, weighted up: a random entity from cty.dat
# is as likely to be Conway Reef as Germany, and the RBN is mostly Europe,
# North America and Japan.
COMMON = ("DL DK DJ DF DO DM DG DH G M 2E GM GW GI EI F ON PA PD I IK IZ IW EA EC OE HB9 OK OM SP SQ SO "
          "UA RA RW RU R UR UT US YO LZ YU 9A S5 HA OH SM SA OZ LA ES YL LY EW ER 4X SV TA CT EU "
          "K W N AA AB KA KB KC KD KE KF KG KI KJ KK VE VA JA JH JR JE JF JG JI JJ BG BA BD HL DS VK ZL "
          "PY PU LU CE CX ZS UN 4L 4S 9M VU YB").split()
NAMES = ("BRIAN JOHN PETE BOB JIM TOM ALEX HANS PETER KLAUS JOSE LUIS MARCO GIANNI JEAN PAUL ANDY STEVE MIKE DAVE "
         "IVAN OLEG JAN PIOTR TONI ERIK OLE LARS KEN TAKA YOSHI RAY BILL ED AL JOE FRANK HERB WOLF UWE").split()
QTHS = ("LONDON BERLIN PARIS MADRID ROMA WIEN PRAHA OSLO HELSINKI BOSTON TEXAS OHIO TOKYO DUBLIN GLASGOW "
        "EDINBURGH MUNICH HAMBURG LYON MILANO KRAKOW KIEV RIGA SOFIA ZAGREB NR LEEDS YORK BRISTOL").split()
RIGS = "IC7300 IC7610 IC705 K3 K4 FT991 FTDX10 FT710 TS590 FLEX KX2 KX3 QCX IC9700 HOMEBREW".split()
ANTS = "DIPOLE YAGI VERT EFHW LOOP INV V GP WIRE BEAM HEXBEAM DELTA LOOP 3EL".split()
WORDS = ("TNX FER CALL UR RST NAME QTH HW CPY RIG ANT PWR WX ES GL GD DX 73 88 OM YL FB SRI QRM QRN QSB "
         "AGN PSE QSL VIA BURO LOTW EQSL TEMP SUNNY RAIN CLOUDY WINDY HR HPE CUL BK TU R RR GM GA GE "
         "NICE SIGS FINE BIZ DR CONDX VY GUD ABT NW QRS QRQ QRL QSY UP DN W 100W 5W KW").split()


@lru_cache(maxsize=1)
def _prefixes() -> tuple[list[str], list[str]]:
    """(prefixes, exact calls) from cty.dat: every alias, overrides stripped."""
    pre, calls = [], []
    text = CTY.read_text(encoding="latin-1")
    for block in text.split(";"):
        if ":" not in block:
            continue
        head, _, aliases = block.rpartition(":")
        for a in aliases.replace("\n", " ").split(","):
            a = re.sub(r"\(.*?\)|\[.*?\]|<.*?>|\{.*?\}|~.*?~", "", a).strip()
            if not a or "VERSION" in a:
                continue
            if a.startswith("="):
                if 3 <= len(a) - 1 <= 12:
                    calls.append(a[1:])
            elif re.fullmatch(r"(?:[A-Z]{1,2}|[A-Z][0-9]|[0-9][A-Z])[0-9]?", a):
                # ITU-shaped only: cty.dat also lists call blocks such as
                # "R8V" or "UA9S" that are not prefixes a call starts from.
                pre.append(a)
    return sorted(set(pre)), sorted(set(calls))


def _letters(rng, n: int) -> str:
    return "".join(chr(ord("A") + int(i)) for i in rng.integers(0, 26, n))


def callsign(rng: np.random.Generator) -> str:
    pre, exact = _prefixes()
    u = rng.random()
    if u < 0.08 and exact:
        call = exact[int(rng.integers(len(exact)))]
    else:
        p = COMMON[int(rng.integers(len(COMMON)))] if u < 0.6 else pre[int(rng.integers(len(pre)))]
        if not p[-1].isdigit():
            p += str(int(rng.integers(0, 10)))
        n = int(rng.choice([1, 2, 3, 4], p=[0.12, 0.3, 0.45, 0.13]))
        call = p + _letters(rng, n)
    v = rng.random()
    if v < 0.05:
        call += "/P"
    elif v < 0.06:
        call += "/QRP"
    elif v < 0.07:
        call += "/M"
    elif v < 0.085:
        call = pre[int(rng.integers(len(pre)))] + "/" + call
    elif v < 0.09:
        call += "/" + str(int(rng.integers(0, 10)))
    return call


def _serial(rng) -> str:
    s = str(int(rng.integers(1, 2500)))
    if rng.random() < 0.3:
        s = s.zfill(3)
    if rng.random() < 0.25:                       # cut numbers
        s = s.replace("0", "T").replace("9", "N")
    return s


def _rst(rng) -> str:
    return str(rng.choice(["5NN", "5NN", "5NN", "599", "579", "559", "589", "ENN", "339", "449", "569"]))


def locator(rng) -> str:
    a = lambda: chr(ord("A") + int(rng.integers(0, 18)))
    b = lambda: chr(ord("A") + int(rng.integers(0, 24)))
    return a() + a() + str(int(rng.integers(0, 10))) + str(int(rng.integers(0, 10))) + b() + b()


def over(rng: np.random.Generator, me: str | None = None, other: str | None = None) -> str:
    """One over: what a station sends between two pauses."""
    c = me or callsign(rng)
    o = other or callsign(rng)
    r = rng.random()
    pick = lambda xs: xs[int(rng.integers(len(xs)))]
    if r < 0.28:
        return pick([f"CQ CQ DE {c} {c} K", f"CQ CQ CQ DE {c} {c} {c} PSE K", f"CQ {c} {c} TEST", f"CQ TEST {c}",
                     f"TEST {c} {c}", f"{c} TEST", f"CQ DX {c} {c} K", f"CQ POTA {c} {c} K", f"CQ SOTA DE {c} {c} K",
                     f"CQ CWT {c}", f"CQ {c} {c}", f"QRZ? {c}", f"CQ CQ {c} {c} {c} K", f"CQ DE {c} {c} <AR>",
                     f"CQ FD {c}", f"CQ CQ DE {c} {c} <AR> K", f"CQ {c}", f"CQ CQ {c}", f"{c} UP", f"CQ DX {c} UP"])
    if r < 0.45:
        n = _serial(rng)
        return pick([f"{o} 5NN {n}", f"{o} {_rst(rng)} {n}", f"TU {c} TEST", f"TU {c}", f"R 5NN {n} TU",
                     f"{o} {_rst(rng)} {int(rng.integers(1, 40))}", f"TU {c} UP", f"{o} 5NN {pick(NAMES)} {n}",
                     f"R TU", f"{o} TU 5NN {n} {c}", f"{c}", f"{c} {c}", f"{o} DE {c} {c} K", f"{o} {c}"])
    if r < 0.70:
        nm, q = pick(NAMES), pick(QTHS)
        return pick([
            f"{o} DE {c} GM OM TNX FER CALL UR RST {_rst(rng)} {_rst(rng)} NAME {nm} {nm} QTH {q} {q} HW CPY? {o} DE {c} K",
            f"{o} DE {c} R R TNX FER RPT UR {_rst(rng)} NAME HR IS {nm} QTH {q} <BT> RIG {pick(RIGS)} ANT {pick(ANTS)} {o} DE {c} KN",
            f"R R TNX FER QSO 73 ES GL {o} DE {c} <SK> E E", f"{o} TU 73 {c} <SK>", f"{o} DE {c} FB OM WX HR {pick(WORDS)} TEMP {int(rng.integers(0, 30))}C <BT> HW? {o} DE {c} K",
            f"{o} DE {c} TNX QSO {pick(NAMES)} 73 GL <SK>", f"QRL? DE {c}", f"{o} DE {c} PSE QSL VIA BURO 73 <SK>",
            f"{o} DE {c} UR {_rst(rng)} {_rst(rng)} IN {q} {o} DE {c} K", f"{o} {o} DE {c} {c} {c} AR",
            f"GM {o} UR {_rst(rng)} OP {nm} QTH {q} LOC {locator(rng)} BK", f"BK TU {_rst(rng)} BK"])
    if r < 0.78:
        n = int(rng.integers(2, 6))
        return " ".join(callsign(rng) for _ in range(n))           # a pileup's calls, or a list
    if r < 0.86:
        n = int(rng.integers(3, 9))
        return " ".join(_letters(rng, int(rng.integers(2, 6))) if rng.random() < 0.7
                        else "".join(str(int(x)) for x in rng.integers(0, 10, int(rng.integers(1, 5))))
                        for _ in range(n))                         # random groups: no language to lean on
    if r < 0.93:
        n = int(rng.integers(3, 10))
        return " ".join(pick(WORDS) for _ in range(n))
    if r < 0.97:
        b = c if c.endswith("/B") else c + "/B"
        return pick([f"VVV VVV DE {b} {b} {locator(rng)}", f"VVV DE {b} {locator(rng)} {locator(rng)}",
                     f"{b} {b} {locator(rng)}"])
    return pick(["EEEEEEEE " + c, f"{c} {c} ?", "QRZ?", "?", f"{o} ?", "AGN AGN", f"{o} 5NN {_serial(rng)} {o} 5NN"])
