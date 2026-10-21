# shack-rec

Recordings made and kept on the OpenHamShack controller, not on the device
that asked for them (Brian, 12/10/2026): receive audio as FLAC with a JSON
sidecar, the recordings folder on the module's data volume with a disk-use
limit and a free-space floor, and HTTP range requests so a player can seek.
QO-100 NB, Radio, Icom and Flex use it; NB and Radio also record IQ (their
engine's SigMF), which this lists, prunes and serves beside the audio.

| Module | What |
|---|---|
| `shack_rec.flac` | `FlacWriter(f, rate, channels, tags)`: streaming FLAC, 16-bit, fixed and LPC predictors, Rice residuals, in numpy. Why FLAC and why our own encoder: its docstring. |
| `shack_rec.audio` | `AudioRecorder`: `push(pcm)` from the audio path (never blocks), `note(freqHz=, mode=)` on each change, a writer thread, the `<id>.json` sidecar. |
| `shack_rec.store` | `Store`: the folder is the database; list, file, delete, `prune(keep)`, `check_room()`. `Refused(status, code, text)`. |
| `shack_rec.recordings` | `Recordings` and `Limits`: the rules every app's API answers by (one of each kind at a time, prune before start and while running, the floor, per-kind longest durations) and the module settings. |
| `shack_rec.http` | `parse_range(header, size)`, `file_headers(...)` for servers without range support of their own (http.server). |

## The API every app with recordings serves

Paths are relative to the app's base (never absolute; honour
`X-Forwarded-Prefix`). Errors are `{"error": "<sentence>", "code"}`.

| Method, path | Level | Does |
|---|---|---|
| `GET api/recordings` | view | `{"recordings": [entry…], …status}`, newest first |
| `GET api/recordings/status` | view | status (below): cheap enough to poll each second while recording |
| `POST api/recordings/start {"kind": "audio" \| "iq"}` | control | starts; `200` status. `400 bad_kind` / `kind_unavailable`, `409 already_recording`, `409 not_running` (the radio is off), `507 low_disk` |
| `POST api/recordings/stop {"kind"?}` | control | stops that kind, or all; `200` status with `"stopped": [active…]` |
| `GET api/recordings/file?id=<id>&part=data\|meta[&download=1]` | view | the file, with `Accept-Ranges: bytes`; `Range: bytes=a-b` answers `206`, past the end `416`. `download=1` makes it an attachment. `404 no_recording` |
| `POST api/recordings/delete {"id"}` | control | deletes both files; `200 {"deleted": entry}`; `409 recording_active`, `404 no_recording` |

Status:

```json
{"recording": true,
 "active": [{"id": "icom-20261012-100000Z-144.300000MHz-USB", "kind": "audio", "format": "flac",
             "startUtc": "2026-10-12T10:00:00Z", "seconds": 12.4, "bytes": 180224,
             "maxSeconds": 14400, "maxBytes": 5000000000, "droppedSeconds": 0.0,
             "freqHz": 144300000, "mode": "USB"}],
 "kinds": ["audio"],
 "last": {"id": "…", "kind": "audio", "stopped": "by the operator", "error": ""},
 "usage": {"bytes": 1234567, "limitBytes": 5000000000, "freeBytes": 80000000000, "floorBytes": 2000000000},
 "limits": {"limitBytes": 5000000000, "floorBytes": 2000000000, "audioMaxSeconds": 14400, "iqMaxSeconds": 600}}
```

Entry (one recording in the list):

```json
{"id": "icom-20261012-100000Z-144.300000MHz-USB", "kind": "audio", "format": "flac",
 "startUtc": "2026-10-12T10:00:00Z", "endUtc": "2026-10-12T10:05:00Z", "seconds": 300.0,
 "sampleRate": 12000, "channels": 1, "freqHz": 144300000, "mode": "USB", "station": "GM4BRI",
 "radio": "IC-9700", "app": "icom", "complete": true, "stopped": "by the operator",
 "recording": false, "bytes": 2101234,
 "parts": {"data": {"name": "….flac", "bytes": 2100000, "type": "audio/flac"},
           "meta": {"name": "….json", "bytes": 1234, "type": "application/json"}}}
```

IQ entries have `"kind": "iq"`, `"format": "sigmf-ci16_le"`, `sampleRate`
the IQ rate, and parts `.sigmf-data` (`application/octet-stream`) and
`.sigmf-meta`; no `channels`. `complete` is false for a recording that
is running or was cut off by a crash (it still plays up to the cut).

Clients: show the Record button only for the kinds in `kinds`; play
`api/recordings/file?id=…&part=data` (FLAC: every browser, ExoPlayer and
AVPlayer play it); warn before IQ (`limits.iqMaxSeconds`, and the app's
own size sentence, e.g. NB's 290 MB a minute at 1.2 MS/s).

## The sidecar (`<id>.json`)

`shackRec` (1), `kind`, `format`, `file`, the app's facts (`app`,
`appName`, `version`, `station`, `radio`, `freqHz`, `mode`, `processing`
e.g. `{"nr2": true, "nr2Level": 50}`), `startUtc`, `endUtc`, `sampleRate`,
`channels`, `sampleFormat` ("s16"), `seconds`, `droppedSeconds`,
`changes` (`[{"t": seconds in, "freqHz"?, "mode"?}]`, the first at 0),
`complete`, `stopped`. The FLAC file's tags carry the start's TITLE,
DATE, FREQUENCY, MODE, STATION, RADIO, ENCODED_BY.

## Settings (module `env:`, one number each)

`RECORDINGS_LIMIT_GB` (the app's default), `RECORDINGS_MIN_FREE_GB` (2),
`RECORDINGS_AUDIO_MAX_HOURS` (4), `RECORDINGS_IQ_MAX_MINUTES` (10). Junk or
0 keeps the default.

## Tests

`tools/test.sh` (own venv). `tests/flacref.py` is a FLAC decoder written
from RFC 9639, so the encoder is checked against the format rather than
itself; with ffmpeg installed the tests also decode with ffmpeg.
