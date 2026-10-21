# shack-client

Every OpenHamShack app's local logbook and station profile (one SQLite
store per user per machine, shared by every app on it), and the sync with
a controller when paired: idempotent pushes by UUID, a change feed pulled
by sequence, field-level last-edit-wins with history, tombstones that a
later edit beats, and nothing lost on unpairing. Also controller discovery
(`_openhamshack._tcp` and the legacy `_openhamshack._tcp`), pairing, and the
controller's side of the sync (`shack_client.controller`) that the Dock360
edition mounts, so client and controller share one merge.

Design: `~/openhamshack/docs/contract/SYNC.md`, `SERVICES.md`,
`CONTROLLER-CONTRACT.md` §2-§4. Stdlib only, Python 3.11+;
`shack-client[discovery]` adds python-zeroconf. GPL-3.0-or-later.
