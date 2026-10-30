# ui/2 worked examples (copied from the contract)

These are the contract's worked examples, copied byte for byte from
openhamshack `docs/contract/screens-fixtures/` at commit 151f2e3
(SCREENS.md §12). They are here so that ohs-libs' vectors are
self-contained: the Python, Kotlin and Swift suites run them from an
ohs-libs checkout alone.

The contract is the original. When it changes, copy the folder again and
run `tools/gen-ui2-vectors.py`, which also says whether these files still
match a given contract checkout:

    tools/gen-ui2-vectors.py --contract ~/wt/screens/docs/contract/screens-fixtures

| File | What |
|---|---|
| `well-known.json` | a controller advertising `services.ui: 2` |
| `launcher.json`, `launcher-status.json` | the platform's launcher document and its status poll answer |
| `apps/shack-radio/app.json` | Radio's descriptor (`ui/v2/app`) |
| `screens/radio/aircraft.json` | Aircraft on a TV |
| `streams/radio/aircraft.jsonl` | its live stream, one `{"atMs", "frame"}` per line: hello, snap, then a `p` frame every 500 ms for a minute |
| `screens/radio/wxsat.json`, `polls/radio/wxsat.json` | Weather Sats and its poll answer |
| `screens/radio/wxsat-viewer.json` | the picture viewer |
