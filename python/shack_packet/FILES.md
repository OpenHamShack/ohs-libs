# Files over packet: the OHS file protocol, YAPP and 7PLUS

Three ways to move a file over amateur packet radio, all in `shack_packet`
(20/10/2026, Packet Radio 0.3.0):

| | Between | Module | Resume | Integrity | Compression |
|---|---|---|---|---|---|
| **OHS** | two OpenHamShack stations, the default | `ohssession`, `filexfer` | yes, by SHA-256, across reconnects and restarts | SHA-256 of the whole file | DEFLATE, negotiated |
| **YAPP / YAPP-C** | any packet software that does YAPP | `yapp` | YAPP-C's RE, by sender + name + size | YAPP-C: 8-bit sum per frame | none |
| **7PLUS** | files posted to and through BBSs as messages | `sevenplus` | collect parts in any order; missing lines named | two CRCs per line | none |

This document is the OHS protocol's specification. Anyone may implement
it; nothing in it is secret, and nothing in it is encryption. A UK amateur
licence (clause 11(2)) forbids messages encrypted to make them
unintelligible; DEFLATE is a published, standard compression that any
listener can undo, and a sender refuses a file that is evidently
encrypted (`filexfer.encrypted_reason`: OpenPGP, age, OpenSSL `Salted__`,
ZIP entries with the encrypted flag).

## 1. Where it travels

An AX.25 connected-mode session between the two stations, information
field PID 0xF0 ("no layer 3"), as one byte stream: the same stream as the
operators' typed text. Nothing uses another PID, so the protocol passes
through a NET/ROM or BPQ node that carries only text. AX.25 already
delivers that stream whole (FCS) and in order (sequence numbers,
retransmission), so the protocol adds no per-frame checksum.

## 2. Frames in the text stream

```
offset  size  field
0       4     magic: 01 4F 48 53  (SOH 'O' 'H' 'S')
4       1     type
5       2     payload length, big-endian, 0..65535
7       n     payload
```

Bytes outside a frame are text and are shown as they arrive. A receiver
scans for SOH; SOH not followed by "OHS" is text. A frame may be cut
across I frames anywhere; a receiver holds the start of one until it is
whole. (SOH is not typed by people or printed by BBSs. YAPP's `ENQ 01`
start is recognised separately so a YAPP receiver can take the stream:
see section 6.)

| type | name | payload |
|---|---|---|
| 1 | HELLO | JSON `{"v": 1, "caps": [...], "app": "..."}` |
| 2 | TEXTZ | raw DEFLATE (RFC 1951) of text bytes |
| 3 | OFFER | JSON `{"id", "name", "size", "sha256", "type", "z"}` |
| 4 | ACCEPT | JSON `{"id", "offset"}` |
| 5 | DECLINE | JSON `{"id", "reason"}` |
| 6 | DATA | binary, below |
| 7 | ACK | JSON `{"id", "have"}` |
| 8 | DONE | JSON `{"id"}` |
| 9 | RESULT | JSON `{"id", "ok", "sha256"?, "reason"?}` |
| 10 | CANCEL | JSON `{"id", "reason"}` |

JSON is UTF-8, compact. Unknown fields are ignored; unknown frame types
are dropped. `id` is 8 lower-case hex digits (4 random bytes), new for
each offer.

## 3. HELLO and what may be sent

A station sends HELLO:

- immediately before its first OFFER on a session;
- in answer to the peer's first HELLO;
- on connecting to a station it has seen send HELLO before (the app keeps
  that list).

Never otherwise: a HELLO sent to a node or BBS would arrive at a command
prompt as junk. `caps`: `"file"` (this protocol), `"z"` (DEFLATE for text
and file data), `"resume"`. After both ends have sent `"z"`, either may
send text as TEXTZ instead of plain bytes - only for 64 bytes or more and
only when it comes out smaller. Each TEXTZ is compressed on its own; a
receiver inflates at most 1 MiB from one.

## 4. A file

```
sender                              receiver
HELLO, OFFER  ------------------->
                                    operator decides (or a held part
                                    of this file is resumed at once)
              <-------------------  ACCEPT offset=N   (or DECLINE)
DATA ... DATA ------------------->  ACK have=... (every 5 %, 1-8 KiB)
DONE          ------------------->  check SHA-256 of the whole file
              <-------------------  RESULT ok=true   (or ok=false)
```

**OFFER**: `name` (as the sender calls it; a receiver must make it safe),
`size` in bytes, `sha256` (64 hex digits, of the whole file), `type` (a
MIME type, informational), `z` (true: DATA payloads will be DEFLATE).

**ACCEPT**: `offset` is how many bytes of this file (same `sha256`, same
`size`) the receiver already holds, 0 for a fresh start. The sender sends
from there.

**DATA**:

```
offset  size  field
0       4     id (the 4 bytes whose hex is the offer's id)
4       8     offset in the file of this chunk's first byte, big-endian
12      1     flags: bit 0 = payload is DEFLATE
13      n     payload
```

Chunks are 1024 file bytes (a sender may choose other sizes; a receiver
must not assume). With `z`, the sender runs one raw-DEFLATE stream for the
run (from the ACCEPT), ending each chunk's payload with a sync flush
(Z_SYNC_FLUSH), so each payload inflates completely on arrival and a
receiver needs one inflater per run. A run that resumes starts a new
stream. A receiver rejects a chunk whose offset is not exactly what it
holds (CANCEL reason `protocol`).

Already-compressed files are sent with `z` false (by name - .jpg, .png,
.zip, .gz, .7z, .mp3, .pdf, .docx and similar - by their first bytes, or
when DEFLATE saves less than 5 % of the first 64 KiB).

**Flow**: a sender keeps no more than two AX.25 windows of I frames
outstanding, so a CANCEL or a typed line is never queued behind the whole
file. **ACK** is progress for the sender's display only.

**DONE / RESULT**: on DONE the receiver hashes everything it holds - the
part from before a resume included - and answers. `ok=false`, reason
`hash-mismatch`: the receiver has thrown its part away; offering again
starts from 0.

**DECLINE / CANCEL reasons**: `declined`, `too-big`, `not-decided`,
`cancelled`, `bad-offset`, `write-failed`, `read-failed`, `protocol`.
A receiver that cancels keeps what it has, so the same file offered again
resumes.

## 5. Resume

A receiver keeps an incomplete file as `.partial/<sha256>.part` in its
inbox folder, with `<sha256>.json` beside it (name, size, sender). When the
link drops, both ends mark the transfer interrupted. When the stations are
connected again and exchange HELLO, the sender offers the file again; the
receiver finds the part by its SHA-256 and size and accepts at its length
without asking its operator again (the app's "resume without asking"
setting, on by default). This survives a restart of either program. The
final SHA-256 check covers the whole file, so a part damaged on disk is
found and discarded.

## 6. YAPP (yapp.py)

For other software. Frame bytes as F6FBB's `yapp.c` (LinFBB 7.04j) and the
FBB documentation's "Yapp-protocols extension" give them (the module's
docstring has the table): SI `ENQ 01`, RR `ACK 01`, HD `SOH len name NUL
size NUL [8 hex DOS date+time NUL]`, RF `ACK 02`, RT `ACK ACK` (YAPP-C),
RE `NAK len 'R' NUL have NUL ['C' NUL]`, NR `NAK len reason`, DT `STX len
data [sum]` (len 0 = 256; YAPP-C appends the 8-bit sum of the data), EF
`ETX 01`/AF `ACK 03`, ET `EOT 01`/AT `ACK 04`, CN `CAN len reason`/CA `ACK
05`. Timeouts as FBB's: 20 s for RR (six tries), 120 s otherwise.

We always send the date field (offering YAPP-C) and answer RT to a header
that has one, RF to one without. A receiver keeps a part under a key made
from the sender's call, the name and the size and answers a later header
for the same file with RE. YAPP holds the session's stream while it runs;
text resumes after AT, CA, or 10 s of nothing after a refusal.

**Not yet tried against BPQ, FBB, WinPack or any other real program**;
the tests play a classic YAPP station from these bytes.

## 7. 7PLUS (sevenplus.py)

Port of DG1BBQ's 7PLUS 2.25 format from its source; the test fixtures were
written by that program built here, and it decoded this encoder's output.
Parts as `NAME.7PL` or `NAME.Pnn` (at most 512 lines and 255 parts), lines
ending LF (CR or CRLF decode the same), 8-bit text: a path that strips bit
7 destroys it, and the decoder says so. There is no whole-file checksum in
7PLUS; each line carries a 14-bit and a 15-bit CRC and the header gives
the size. Correction files (.ERR/.COR) are not made or read.
