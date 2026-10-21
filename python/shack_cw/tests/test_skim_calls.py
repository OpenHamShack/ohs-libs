"""The skimmer's callsigns and spots (shack_cw.skim.calls): what makes a
spot, what is busted, and what is never a spot - on words as a channel's
decoder prints them."""
from shack_cw.skim import calls
from shack_cw.skim.calls import Calls, classify, shape_ok, similar

# A stand-in for an app's cty.dat lookup: a few prefixes, so a call from
# nowhere ("QQ1ABC") is refused the way the real lookup refuses it.
_PREFIXES = {"DL": "Germany", "G": "England", "GM": "Scotland", "OK": "Czech Republic", "F": "France",
             "K": "United States", "W": "United States", "JA": "Japan", "OH": "Finland", "4X": "Israel",
             "VK": "Australia", "EA": "Spain", "EA8": "Canary Islands"}


def country(call):
    home = calls.home_of(call)
    for n in (3, 2, 1):
        name = _PREFIXES.get(home[:n])
        if name:
            return {"country": name, "dxcc": home[:n], "continent": "EU"}
    return None


# A stand-in for MASTER.SCP holding the calls these tests use, so that two
# decodes confirm them (calls.SEEN); a call outside it needs three
# (calls.UNLISTED_SEEN).
LISTED = {"DL1ABC", "GM4BRI", "GM4BRS", "OK1AB", "OH2B", "SM5D", "IZ5FDE", "UA6HNU", "LA9OI", "OE7AJI", "G4ABC"}


def listed(call):
    return call in LISTED


def feed(c, track, text, f=14025000.0, t0=0.0, step=1.0, wpm=24.0, snr=12.0):
    out = []
    t = t0
    for w in text.split():
        out += c.word(track, w, f, t, wpm=wpm, snr=snr, band="20m")
        t += step
    out += c.flush(track, f, t, wpm=wpm, snr=snr, band="20m")
    return out


def last(spots, call):
    got = [s for s in spots if s["call"] == call]
    return got[-1] if got else None


def test_shapes():
    for good in ("GM4BRI", "DL1ABC", "K1A", "4X1AB", "EA8/G4ABC", "G4ABC/P", "OH2B/B", "JA1ABC", "9A2AB"):
        assert shape_ok(good), good
    # A locator, the stop list, a bare prefix, the decoder's unknown letter,
    # a word with no digit, special-event shapes the skimmer refuses.
    for bad in ("IO75RL", "5NN", "TEST", "EA8", "GM4B*I", "CQ", "TU", "ABCDEF", "73", "GB100RSGB", ""):
        assert not shape_ok(bad), bad


def test_morse_similar():
    assert similar("GM4BRI", "GM4BRS")      # I and S: dits
    assert similar("OK1AB", "OK1AD")        # B and D: -... and -..
    assert similar("GM4BRI", "GM4BRIE")     # one E gained
    assert not similar("GM4BRI", "GM4BRI")
    assert not similar("GM4BRI", "DL1ABC")
    assert not similar("OK1AB", "OK1XY")    # two letters apart


def test_classify_patterns():
    w = "CQ CQ DE DL1ABC DL1ABC K".split()
    assert classify(w, 3) == "cq" and classify(w, 4) == "dx"
    assert classify("CQ TEST OK1AB".split(), 2) == "cq"
    assert classify("OK1AB TEST".split(), 0) == "cq"
    assert classify("TU GM4BRI".split(), 1) == "dx"
    assert classify("VVV VVV DE OH2B".split(), 3) == "beacon"
    # <OTHER> DE <ME>: the other station is being worked from elsewhere.
    assert classify("GM4BRI DE DL1ABC".split(), 0) == "worked"
    assert classify("GM4BRI GM4BRI DE DL1ABC".split(), 0) == "worked"
    # A call with no context is no reason to spot.
    assert classify("UR 5NN DL1ABC BK".split(), 2) is None


def test_two_hits_make_a_valid_spot():
    c = Calls(country=country, history=listed)
    spots = feed(c, 1, "CQ CQ DE DL1ABC DL1ABC K")
    s = last(spots, "DL1ABC")
    assert s["status"] == "valid" and s["kind"] == "cq" and s["hits"] == 2
    assert s["country"] == "Germany" and s["mode"] == "cw"
    assert s["dial"] == s["f"] - 700.0
    assert s["snr"] == 12.0 and s["wpm"] == 24.0


def test_one_hit_is_unconfirmed_even_for_a_listed_call():
    # The spot rule (Brian, 14/10/2026): the callsign check AND seen twice.
    # A call in the list still needs its second decode.
    c = Calls(country=country, history=lambda call: call == "OK1AB")
    s = last(feed(c, 1, "CQ TEST OK1AB"), "OK1AB")
    assert s["status"] == "unconfirmed"
    s = last(feed(c, 1, "CQ TEST OK1AB", t0=30.0), "OK1AB")
    assert s["status"] == "valid" and s["hits"] == 2


def test_a_call_the_list_does_not_hold_needs_a_third_decode():
    # With a list loaded, a call outside it stands on its shape and DXCC
    # entity alone, so it must be seen UNLISTED_SEEN times.
    assert calls.UNLISTED_SEEN == 3
    c = Calls(country=country, history=lambda call: call in {"OK1AB"})
    s = last(feed(c, 1, "CQ CQ DE DL1ABC DL1ABC K"), "DL1ABC")
    assert s["status"] == "unconfirmed" and s["hits"] == 2
    s = last(feed(c, 1, "CQ DE DL1ABC K", t0=30.0), "DL1ABC")
    assert s["status"] == "valid"
    # Confirmed once, it counts as listed for the rest of the session:
    # two decodes confirm it at its new frequency.
    s = last(feed(c, 2, "CQ CQ DE DL1ABC DL1ABC K", f=14031000.0, t0=60.0), "DL1ABC")
    assert s["status"] == "qsy" and s["hits"] == 5


def test_unlisted_calls_can_be_refused_outright(monkeypatch):
    monkeypatch.setattr(calls, "UNLISTED_SEEN", None)
    c = Calls(country=country, history=lambda call: call in {"OK1AB"})
    s = last(feed(c, 1, "CQ CQ DE DL1ABC DL1ABC K CQ DE DL1ABC DL1ABC"), "DL1ABC")
    assert s["status"] == "unconfirmed" and s["hits"] == 4


def test_with_no_list_every_call_needs_a_third_decode():
    # history() answering None is "no list yet" (scp.Scp before its file
    # is in), the same as no history at all: the shape and DXCC test,
    # seen three times.
    for hist in (None, lambda call: None):
        c = Calls(country=country, history=hist)
        assert last(feed(c, 1, "CQ CQ DE DL1ABC DL1ABC K"), "DL1ABC")["status"] == "unconfirmed"
        assert last(feed(c, 1, "CQ DE DL1ABC K", t0=30.0), "DL1ABC")["status"] == "valid"


def test_a_decode_by_each_engine_is_seen_twice():
    # Two engines on one channel: the same call printed once by each.
    c = Calls(country=country, history=listed)
    c.word(1, "CQ", 14025000.0, 0.0, engine="nn")
    c.word(1, "DE", 14025000.0, 1.0, engine="nn")
    c.word(1, "DL1ABC", 14025000.0, 2.0, engine="nn")
    out = c.flush(1, 14025000.0, 3.0, engine="nn")
    assert last(out, "DL1ABC")["status"] == "unconfirmed"
    for i, w in enumerate("CQ DE DL1ABC".split()):
        c.word(1, w, 14025010.0, 0.5 + i, engine="classic")
    s = last(c.flush(1, 14025010.0, 3.5, engine="classic"), "DL1ABC")
    assert s["status"] == "valid" and s["hits"] == 2


def test_one_engines_letters_never_finish_the_others_call():
    # Each engine's words are a stream of their own: the network printing
    # "CQ DE DL1" and the classic engine "ABC K" on the same track is not
    # DL1ABC, though one stream's "DL1 ABC" would be joined by segment().
    c = Calls(country=country, history=listed)
    for i, w in enumerate("CQ DE DL1".split()):
        c.word(1, w, 14025000.0, float(i), engine="nn")
    for i, w in enumerate("ABC K".split()):
        c.word(1, w, 14025000.0, 3.0 + i, engine="classic")
    out = c.flush(1, 14025000.0, 6.0, engine="nn") + c.flush(1, 14025000.0, 6.0, engine="classic")
    assert last(out, "DL1ABC") is None
    c = Calls(country=country, history=listed)
    out = []
    for i, w in enumerate("CQ DE DL1 ABC K CQ DE DL1 ABC K".split()):
        out += c.word(1, w, 14025000.0, float(i), engine="nn")
    out += c.flush(1, 14025000.0, 10.0, engine="nn")
    assert last(out, "DL1ABC")["status"] == "valid"
    # Forgetting the track forgets both streams.
    c.word(1, "CQ", 14025000.0, 20.0, engine="nn")
    c.word(1, "CQ", 14025000.0, 20.0, engine="classic")
    c.forget(1)
    assert not c._raw and not c._words


def test_the_worked_station_is_not_spotted():
    c = Calls(country=country, history=listed)
    spots = feed(c, 1, "GM4BRI GM4BRI DE DL1ABC DL1ABC TU 5NN K")
    assert last(spots, "GM4BRI") is None
    assert last(spots, "DL1ABC")["status"] == "valid"


def test_no_country_no_spot():
    c = Calls(country=country, history=listed)
    assert feed(c, 1, "CQ CQ DE QQ1ABC QQ1ABC K") == []


def test_busted_copy_beside_a_confirmed_call():
    c = Calls(country=country, history=listed)
    feed(c, 1, "CQ CQ DE GM4BRI GM4BRI K CQ DE GM4BRI GM4BRI K", f=14025000.0)
    # The same signal, read once with an I as an S, on a track 40 Hz away.
    spots = feed(c, 2, "CQ CQ DE GM4BRS K", f=14025040.0, t0=20.0)
    s = last(spots, "GM4BRS")
    assert s["status"] == "busted" and s["bustedOf"] == "GM4BRI"
    # Two real stations 2 kHz apart with alike calls: neither is busted.
    spots = feed(c, 3, "CQ CQ DE GM4BRS GM4BRS K", f=14027000.0, t0=40.0)
    assert last(spots, "GM4BRS")["status"] == "valid"


def test_a_late_winner_busts_an_earlier_copy():
    c = Calls(country=country, history=listed)
    feed(c, 2, "CQ DE GM4BRS K", f=14025040.0)            # one bad copy first
    spots = feed(c, 1, "CQ CQ DE GM4BRI GM4BRI GM4BRI K", f=14025000.0, t0=10.0)
    s = last(spots, "GM4BRS")
    assert s is not None and s["status"] == "busted"


def test_qsy_and_beacon_and_rate():
    c = Calls(country=country, history=listed)
    feed(c, 1, "CQ CQ DE DL1ABC DL1ABC K", f=14025000.0)
    spots = feed(c, 5, "CQ CQ DE DL1ABC DL1ABC K", f=14031000.0, t0=60.0)
    assert last(spots, "DL1ABC")["status"] == "qsy"
    # A call on an NCDXF beacon frequency is a beacon.
    s = last(feed(c, 7, "DE OH2B OH2B", f=14100000.0), "OH2B")
    assert s["kind"] == "beacon"
    # Hits past four, same status and kind, within 10 minutes: nothing new.
    c = Calls(country=country, history=listed)
    feed(c, 1, "CQ DE DL1ABC DL1ABC K CQ DE DL1ABC DL1ABC K")
    assert feed(c, 1, "CQ DE DL1ABC DL1ABC K", t0=30.0) == []
    revs = [s["rev"] for s in feed(c, 1, "CQ DE DL1ABC DL1ABC K", t0=700.0)]
    assert revs, "re-spotted after RESPOT_S"


def test_list_and_kept_window():
    c = Calls(country=country, history=listed)
    feed(c, 1, "CQ DE DL1ABC DL1ABC K", t0=0.0)
    feed(c, 2, "CQ DE OK1AB OK1AB K", f=14030000.0, t0=100.0)
    assert [s["call"] for s in c.list(120.0)] == ["OK1AB", "DL1ABC"]
    assert [s["call"] for s in c.list(100.0 + calls.KEEP_S + 10)] == []
    assert c.call_on(2) == ("OK1AB", "cq")


def test_real_fists_glued_and_split_words():
    # The decoders' word spaces on real bands (the box's 20 m recording,
    # the Kiwi corpus): CQ and DE run onto the call, a prosign in front,
    # a call broken by a letter gap read as a word gap.
    assert calls.tokens("CQDESM5D") == ["CQ", "DE", "SM5D"]
    assert calls.tokens("TIQCQDESM5D") == ["TIQ", "CQ", "DE", "SM5D"]
    assert calls.tokens("<BT>DEAA9H") == ["<BT>", "DE", "AA9H"]
    assert calls.tokens("TUNE") == ["TUNE"] and calls.tokens("IDEA") == ["IDEA"]
    c = Calls(country=country, history=listed)
    s = last(feed(c, 1, "TIQCQDESM5D GM4BRI TIQCQDESM5D K", f=14025200.0), "SM5D")
    assert s is None   # SM5D is no entity in this lookup: refused, not mangled
    c = Calls(country=country, history=listed)
    s = last(feed(c, 1, "CQDE OK1AB K CQ DE OK 1AB K"), "OK1AB")
    assert s["status"] == "valid" and s["kind"] == "cq" and s["hits"] == 2


def test_segment_respaces_what_the_decoders_printed():
    # Each case as a channel printed it on the box's 20 m recording or the
    # Kiwi corpus (shack_cw.skim.recovery), with this file's prefixes plus
    # the ones those calls need.
    known = dict(_PREFIXES, IZ="Italy", UA="Russia", SH="Sweden", SM="Sweden", OE="Austria", LA="Norway",
                 A9="Bahrain")

    def is_call(s):
        home = calls.home_of(s)
        return calls.shape_ok(s) and any(home[:n] in known for n in (3, 2, 1))
    seg = lambda text: calls.segment(text.split(), is_call)
    assert seg("TIQCQDESM5D") == ["TIQ", "CQ", "DE", "SM5D"]       # word spaces not heard
    assert seg("I Z 5FDE") == ["IZ5FDE"]                           # letter gaps heard as spaces
    assert seg("U A 6 H N U") == ["UA6HNU"]                        # every one of them
    assert seg("TEST SH2 MAY") == ["TEST", "SH2MAY"]
    assert seg("DE OE 7AJI") == ["DE", "OE7AJI"]
    # A call sent twice stays two calls, not a call, a DE and a call.
    assert seg("CQCQCQIZ5FDE IZ5FDE") == ["CQ", "CQ", "CQ", "IZ5FDE", "IZ5FDE"]
    # What was printed whole stays whole: no call is made of a keyword,
    # and a call is not lengthened by a stray letter.
    assert seg("TU 5NN K") == ["TU", "5NN", "K"]
    assert seg("E G4ABC") == ["E", "G4ABC"]
    # ... unless the words round it say so: a call after DE, sent twice.
    assert seg("DE L A9OI L A9OI") == ["DE", "LA9OI", "LA9OI"]
    assert seg("TUNE") == ["TUNE"] and seg("IDEA") == ["IDEA"]
    # Without a digit there is no call to make: noise stays as printed.
    assert seg("E E T E") == ["E", "E", "T", "E"]


def test_a_call_spelt_out_letter_by_letter_is_spotted():
    # UA6HNU on the box: every letter gap read as a word space, the call
    # whole only once its last letter is in - six words after the DE.
    known = dict(_PREFIXES, UA="Russia")

    def ctry(call):
        home = calls.home_of(call)
        for n in (2, 1):
            if home[:n] in known:
                return {"country": known[home[:n]], "dxcc": home[:n], "continent": "EU"}
        return None
    c = Calls(country=ctry, history=listed)
    s = last(feed(c, 1, "CQ CQ DE U A 6 H N U U A 6 H N U K"), "UA6HNU")
    assert s is not None and s["status"] == "valid" and s["kind"] == "cq"
