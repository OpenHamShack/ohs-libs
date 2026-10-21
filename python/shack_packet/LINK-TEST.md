# Link test with a friend: the return path, the relay and the telemetry

Contract version 1, 18/10/2026; web protocol 1, 20/10/2026. Code:
`shack_packet.returnpath` (the engine hook), `shack_packet.linktest` (the
client and the telemetry), `shack_packet.weblink` (the client for
openhamshack.com), `tools/packet_relay.py` (the TCP relay),
`tools/packet-relay.service`; the web relay is openhamshack-os
`server/link`.

## Why

Two stations, each with a Pluto and one antenna and no T/R switch. A run
is one RF direction: the **RF sender** has its antenna on the Pluto's TX
port, the **RF receiver** on its RX port. Connected mode needs the
receiver's link-layer answers (UA, RR, RNR, REJ, SREJ, XID, DM, DISC)
back at the sender, so they go by internet through a relay on the
OpenHamShack controller box. Both stations also stream what they know
about the link, live to each other and into the relay's log.

## Rules

- **Off by default, never silent.** An engine has no return path until
  the app attaches one, for one remote station (call and SSID). Attach,
  detach and loss are engine events (`return-path`: on, off, lost) and
  log warnings. Every frame through it is counted and reported with
  `via: "internet"`; the app shows a banner while one is attached.
- **The RF receiver** sends every frame for that station by return
  path (I frames included: it has no TX antenna) and none by radio; its
  station has transmit off as well. It answers once its receiver has
  heard no carrier for 50 ms (the over has ended).
- **The RF sender's** RF path is unchanged. It takes frames from the
  return path as if heard, but only from that station to one of its own
  addresses, with no level or mode (adaptation never uses them).
- **Fail safe.** The relay closing, 5 s without a word from it, or the
  other station leaving loses the return path: every link to that station
  drops at once without transmitting (no DISC, no DM), frames to it still
  queued for the radio are discarded, and `connect()` to it is refused
  while the path is down. At most the over already under way finishes.
  Losing the relay ends the run (join again). The other station leaving
  does not: the station still there stays joined, goes back to `waiting`
  (why `peer-left` or `peer-silent`) and is `ready` again, with
  `connect()` allowed, when a station rejoins in that role (from
  0.2.3).
- **Outwards only.** Both apps connect to the relay; neither listens.
- **Tailnet only.** The relay listens on 127.0.0.1:30480 on the box and
  `tailscale serve --tcp 30480` publishes it to the tailnet.

## Protocol 1

TCP, one JSON object per line, UTF-8, at most 64 KiB a line. Every message
a client sends carries its own `mono` (monotonic seconds) and `wall`
(Unix seconds).

| from | `t` | fields | what happens |
|---|---|---|---|
| client | `hello` | `v` 1, `session` (3-40 of `A-Za-z0-9_-`), `role` (`rf-sender` / `rf-receiver`), `call`, `peer`, `env` | first line; relay answers `welcome` or `error` |
| relay | `welcome` | `session`, `role`, `peer` (the other station's hello, or null), `log` (file name), `relay_wall`, `relay_mono` | joined |
| relay | `error` | `reason`: `hello-first`, `protocol`, `bad-session`, `bad-role`, `role-taken`, `too-many-sessions` | then closes |
| client | `ping` | `n` | every second; relay answers `pong` |
| relay | `pong` | `n`, `wall` (the ping's), `relay_wall`, `relay_mono` | heartbeat and clock offset |
| client | `frame` | `n`, `hex` (the AX.25 frame without FCS), `monitor` (a one-line summary, never payload) | forwarded to the other station |
| client | `tele` | `kind` and its fields (below) | forwarded to the other station |
| relay | `undeliverable` | `n` | a frame with nobody to take it |
| relay | `peer` | `state` `joined` + `hello`, or `left` + `why` (`bye`, `closed`, `timeout`) | the other station came or went |
| client | `bye` | `why` | leaving |

A client silent for 5 s is dropped as `timeout`.

## Web protocol 1 (openhamshack.com, from 0.3.0)

Why: the TCP relay is on the tailnet only. Every station can reach
openhamshack.com over HTTPS, which is shared hosting with no long-running
processes, so the relay there is one PHP endpoint and each station makes
requests. `POST https://openhamshack.com/api/link/link.php`, a JSON body
with `op`; answers are JSON with `ok`, and on failure `error`. The
messages relayed are this protocol's own (`frame`, `tele`, ...) with
their `mono`/`wall` stamps; `ping`/`pong` are not used (an answer carries
`relay_wall`).

| op | body | answer |
|---|---|---|
| `hello` | `v` 1, `call`, `peer`, `token` (32 hex, new each run), `version`, `env`, `wall` | `pair`, `call`/`peer_call` (normalised), `first` (the call that sorts first), `cursor` 0, `peer` (below), `relay_wall`, `poll_max_s` |
| `send` | `call`, `peer`, `token`, `msgs` (at most 400; a line at most 64 KiB) | `n`, `delivered` (false: the other station is not there; dropped), `relay_wall` |
| `poll` | `call`, `peer`, `token`, `cursor`, `wait` (held at most 20 s) | `msgs` after the cursor, the new `cursor`, `peer`, `relay_wall` |
| `bye` | `call`, `peer`, `token`, `why` | `ok` |

`peer` is `{"state": "absent"}` or `{"state": "here", "gen", "hello",
"seen_s"}` (seconds since the server last heard from it). The relay adds
`{"t": "peer", "state": "joined", "gen", "hello"}` and `{"t": "peer",
"state": "left", "why"}` (`bye`, `stopped`, `replaced`...) to a station's
messages; a station never sends `peer`, `welcome` or `error`.

- **Pairing:** calls are upper-cased, `-0` dropped, SSIDs kept; a pair is
  the two calls, so it exists only when each asked for the other.
- **Token:** only its SHA-256 is stored; a wrong one answers 409
  `superseded`. A hello with a new token replaces that call's run (its
  queue emptied, the other station told `left` then `joined`).
- **Silence:** the client posts at least every 2 s; nothing answered for
  8 s loses the return path (as the TCP relay closing does). The other
  station is gone when `seen_s` passes 15 and back when it falls again.
- **Roles are the app's**: one pairing carries both halves of a run.
- **Errors:** 400 `bad-request`/`bad-call`/`same-call`/`bad-token`/
  `bad-message`/`protocol`, 405 `post-only`, 409 `superseded`/`left`, 413
  `body-size`/`message-too-big`, 429 `too-fast` (900 requests a station a
  minute)/`too-many-hellos` (60 an address an hour), 503 `busy` (200
  pairs), 507 `queue-full` (16 MiB a station; start again).
- **Log:** `~/ohs-link/logs/<UTC date>-<pair>.jsonl` on the site's host,
  outside the web root, mode 0600, one line per message (`relay_wall`,
  `from` (the call), `lag_s`, `msg`) and per join/leave; kept 30 days.
  Idle pairs go after an hour.

## Telemetry kinds

| kind | when | fields |
|---|---|---|
| `env` | once | `app`, `shack_packet`, `os`, `python`, `machine`, `call`, `role`, `radio` {kind, uri, frequency, mode, modes, baud, tx_gain_db, rx_gain, xo_applied, identity, transmit, guard_state, ifo_hz, rate_iq, deviation_hz, turnaround_s, hw_model, hw_serial, fw_version...} |
| `rx-frame` | each frame heard | `via` (radio/internet), `mode`, `cn0_dbhz` (from the SDR receiver's level, adaptive or one mode; None from a rig), `snr_db` (in the mode's occupied bandwidth; None for a mode with none), `level_db`, `offset_hz`, `fcs` ("ok": failures never reach the engine, see `rx`), `decode_lag_s`, `type`, `ns`, `nr`, `pf`, `src`, `dst`, `len`, `frame`, `at` (engine clock) |
| `tx-over` | each transmission, once played | `start`, `end`, `duration_s`, `frames`, `types`, `mode`, `modes`, `counters` (changes in tx_late, tx_late_ms, tx_gaps, keyed, refused, watchdog, radio-error), `tx_slack_min_ms`, `tx_gain_db`, `xo`, `temp_c` (at most every 30 s, own iio context), `guard`, `first` |
| `adapt` | adapter start and each decision | `remote`, `at`, `request`, `why`, (`ladder` at start) |
| `link` | each engine event | `event`, `reason`, `remote`, `detail` |
| `link-stats` | each second, each connection | `state`, `vs`, `va`, `vr`, `rc`, `srt_s`, `t1v_s`, `queued`, `k`, `n1`, `modulo`, `srej`, `tx_mode`, `peer_busy`, `counters` (changes: t1_expired, retransmitted, sent_*), `sent_bytes`, `received_bytes`, `via_return_path` |
| `rx` | each second | `receivers` {name: frames, fx25, bad_fcs}, `heard_by_mode`, `engine` (heard, own_echo, rp_*), `channel` (transmissions, frames, airtime_s, refused), `level_db`, `rx_dropped` |
| `clock` | every 10 s | `relay_minus_local_s` and `rtt_s` from the lowest-RTT pong of the last minute, `samples` |

Values that are not finite numbers are null. Fields a radio cannot give
are null, never absent.

## The relay's log

`/var/lib/ohs-packet-relay/sessions/<UTC date>-<session>.jsonl`, one line
per message received and per relay event:

```json
{"relay_wall": 1792345678.123456, "relay_mono": 1234.567891, "from": "rf-sender", "lag_s": 0.0213, "msg": {...}}
```

`from` is the role or `relay` (`join`, `leave`, `refused`, `undeliverable`,
`summary`, `relay-stopping`). `lag_s` = relay_wall - msg.wall. When the
last station leaves, a `summary` line and `<name>.summary.json`: calls,
joins, duration, messages by role and type, frames each way, telemetry by
kind, last clock estimates.

Each app also keeps its own copy of every message in and out, in its data
folder (`linktest/<session>-<role>.jsonl` in Packet Radio), each line
`{"dir": "in"|"out"|"event", "mono", "wall", "msg"}`.

To put both stations on one time axis: a station's wall + its latest
`relay_minus_local_s` is relay time.
