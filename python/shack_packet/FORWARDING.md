# Store-and-forward: the FBB forwarding exchange

Contract version 1, 20/10/2026. Code: `shack_packet.fbb` (both roles,
the store-agnostic state machine), `shack_packet.bbs` (the answering BBS),
`shack_packet.bbsstore` (what a BBS proposes and accepts). Users: the
OpenHamShack controller's BBS (store app `shack-packet`), the desktop's
store-and-forward client (Packet Radio 0.3.0 workstream E), and later the
box's forwarding to UK GB7 BBSs.

**[guess]** marks what has not been checked against a real FBB or
BPQMail. Nothing here has been on the air: the tests run two stations on
the simulator (`tests/test_bbs*.py`, `tests/test_fbb.py`).

## 1. Why FBB

It is the exchange every packet BBS still speaks: FBB defined it, BPQMail
(the GB7 boxes) and Winlink's gateways speak dialects of it. The ASCII
form (no compression) is the one all of them accept, so an OpenHamShack
station and the controller use exactly that, and the controller can
forward with an existing BBS without a gateway. Compressed forwarding
(FBB `B`, `B1`, Winlink `B2`) is a later speed-up, never a different
protocol: a station that offers only `F` gets ASCII from everybody.

Nothing is encrypted, and no password crosses the air (UK licence: no
encryption to obscure meaning). Identity is the AX.25 source callsign, as
on every packet BBS.

## 2. The link and the lines

- One AX.25 connected-mode session (`Engine.connect`), the caller to the
  BBS's address (the controller's BBS: `CALL-1` by default).
- Text is lines ended by CR. A receiver also takes LF and CR LF as one
  end of line. UTF-8 is sent; a line that is not UTF-8 is read as
  Latin-1 (older BBSs).
- Every line in the exchange is ASCII except subjects and message text.
- **Ctrl-Z** (0x1A) ends a message. It is sent alone on a line (`\x1a\r`);
  a receiver also takes it at the end of the last text line.

## 3. The SID

A line `[NAME-VERSION-FEATURES]`, the features last and ending in `$`
when BIDs are supported. Ours: **`[OHS-0.3.0-FHM$]`**.

| Letter | Means | We |
|---|---|---|
| `F` | FBB proposals (`FB`/`FS`/`FF`/`FQ`) | send and require |
| `H` | hierarchical addresses (`@GB7XXX.#79.GBR.EURO`) | send |
| `M` | message ids (MIDs) | send |
| `$` | bulletin ids (BIDs); always last | send |
| `B`, `B1`, `B2` | compressed forwarding | not in v1: ignored |
| anything else (`A`, `I`, `J`, `W`...) | other software's | ignored |

Parsing: the line starts with `[` and ends with `]`; the text between is
split on `-`; the **last** field is the features (BPQ's
`[BPQ-6.0.24.1-B1FWIHJM$]` has a dotted version; Winlink's
`[WL2K-5.0-B2FWIHJM$]`). A SID without `F` is not a forwarding partner:
the caller sends `B` (bye) and disconnects.

## 4. The exchange

```
caller (station)                         answerer (BBS)
       ── SABM / UA ──────────────────────────
                                    [OHS-0.3.0-FHM$]
                                    (greeting lines)
                                    GM4BRI-1 BBS>          <- a line ending in ">"
[OHS-0.3.0-FHM$]
FB P G0ABC GM4BRI GM4BRI 17_G0ABC 112                       <- caller proposes first
F>
                                    FS +
Sked tonight?                                               <- subject
R:261020/1432Z 17@G0ABC [Leeds] OHS0.3.0                    <- optional R: lines
                                                             
Are you on 2 m at 2000?                                     <- text
^Z
                                    FB P G4XYZ GM4BRI G0ABC 52_GM4BRI 80   <- answerer's turn
                                    F>
FS +
                                    Re: lunch
                                    ...
                                    ^Z
FF                                                           <- caller: nothing more
                                    FQ                       <- answerer: nothing either; ends
       ── DISC / UA ───────────────────────────
```

1. **Answerer, on connect:** its SID line, any greeting, then a prompt line
   ending in `>`.
2. **Caller:** waits for a line ending in `>` that comes after the
   answerer's SID (at most 60 s), then sends its SID. No `F` in the
   answerer's SID: `B`, disconnect.
3. **The caller proposes first**: a block (§5) or `FF`.
4. Then turns alternate. The side that has just received a block answers
   it (`FS`), receives the accepted messages, and then it is that side's
   turn: its own block, or `FF` if it has nothing.
5. **`FF` received:** a side with messages proposes a block; a side with
   none sends `FQ`.
6. **`FQ`:** the side that sent it closes the link once the line has
   been acknowledged (`Connection.close`, which drains); the side that
   received it closes too. Neither waits more than 30 s for the other.
7. A side whose turn it is never waits on the other: each turn is one of
   *block*, `FF` or `FQ`, and anything else in its place is a protocol
   error.

## 5. A block of proposals

At most **5** proposals, then `F>`:

```
FB <type> <from> <at> <to> <bid> <size>
...
F>
```

| Field | Rule |
|---|---|
| `type` | `P` personal, `B` bulletin. `T` (NTS traffic) is answered `-`. |
| `from` | the sender's callsign, no SSID, upper case |
| `at` | where it is going: the destination BBS's call with optional hierarchy (`GM4BRI`, `GB7XXX.#79.GBR.EURO`) for personal mail; the distribution area for a bulletin (`GBR`, `EURO`, `WW`). Never empty: a station with no idea sends the answering BBS's call. |
| `to` | the addressee's callsign (no SSID), or the bulletin's topic (`ALL`, `SALE`, `KEPS`, at most 6 characters) |
| `bid` | the message's BID/MID: `[A-Z0-9_-]`, 1 to 12 characters, unique for ever from its origin. We make `<number>_<CALL>` and cut the call so it fits. |
| `size` | the text's length in bytes, a hint only |

Fields are separated by one or more spaces; a receiver ignores a sixth
space and anything after `size` (FBB adds nothing; Winlink's `FC` lines
are a different command and are answered as a protocol error in v1).

`F>` ends the block. FBB's compressed modes put a checksum after it (`F>
HH`); in ASCII forwarding we send none and accept one, checked if present:
the two's complement of the sum of every byte of the block's `FB` lines,
CRs included, as two hex digits **[guess: from FBB's protocol notes, not
yet checked against BPQMail]**.

## 6. The answer

`FS ` and one character per proposal, in order:

| Char | Means | Also accepted on input |
|---|---|---|
| `+` | send it | `Y` |
| `-` | no: already have it (BID seen), or refused | `N`, `R` |
| `=` | not now (it is coming from somewhere else, or a limit for today) | `L` |

What our answerer refuses (`-`): a BID it has ever stored; `T`; a size over
the per-message limit; a caller that is not a listed partner offering a
bulletin, or personal mail for a call whose `at` is not this BBS (no
third-party relaying through the box by strangers); a barred call. A
daily limit reached is `=`.

## 7. A message

For each `+`, in order: the subject on one line, then the text, then
Ctrl-Z. The text may begin with routing lines, one per BBS the message
passed through, newest first:

```
R:261020/1432Z 17@GM4BRI.#79.GBR.EURO [Edinburgh] OHS0.3.0
```

(`R:` date `yymmdd/hhmmZ`, the message number at that BBS `@` its
hierarchical address, its QTH in brackets, its software). A BBS adds its
own line when it forwards a message on, and keeps those it received.
Stations need not add one.

## 8. When a message counts as delivered

FBB has no per-message acknowledgement. **A message counts as delivered
when the receiver's next turn arrives** (its block, `FF` or `FQ`) after the
message's Ctrl-Z. A link lost before that leaves the message undelivered;
it is proposed again next time, and the receiver's BID check answers `-`
if it had in fact kept it, so nothing arrives twice. The receiver stores a
message only once its Ctrl-Z has arrived (a half message is dropped).

## 9. The OpenHamShack station (workstream E)

A desktop station delivers and collects its mail at its controller's BBS:

- **Deliver:** propose each outgoing message: `P`, `at` = the addressee's
  home BBS if known, else the controller's call; BID `<n>_<MYCALL>`.
  Personal mail for a local user (anyone who has mail kept here or has
  connected) or for a station that collects here is kept; for elsewhere,
  only from a listed partner (§6), so a station sends such mail to its
  own controller first only when that controller lists it as a partner.
- **Collect:** after the station's turn, the BBS proposes every personal
  message whose `to` is the station's call (no SSID) that it has not yet
  delivered to that call. Each delivered one (§8) is marked forwarded on
  the BBS: it leaves the BBS's lists and is purged with read mail.
- The station's own SID is `[OHS-<version>-FHM$]`. Any software with `F`
  may do the same (an FBB "point" such as Outpost or Paclink).

## 10. Errors and limits

- A line that is not what the turn allows: send `*** Protocol error`,
  then disconnect. Partial messages are dropped; nothing is marked
  delivered.
- Idle: 120 s waiting for the other side's turn or for message text,
  then disconnect.
- Sizes: a proposal's message larger than the answerer's limit is `-`; a
  message whose text grows past 2 x its proposed size or past the limit
  is a protocol error.

## 11. Not in version 1

Compressed forwarding (B/B1/B2, LZHUF), resume (`!offset`), `FA`/`FC`
proposals, NTS traffic, hierarchical routing tables beyond "partner X
takes routes matching these patterns", connect scripts through nodes
(a partner is reached directly or through digipeaters).
