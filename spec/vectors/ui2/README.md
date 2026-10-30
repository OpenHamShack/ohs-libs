# ui/2 vectors

What every implementation of server-driven screens (openhamshack
`docs/contract/SCREENS.md`) is held to: `shack_ui` in Python (the
reference), the Android renderer (Kotlin) and the Apple renderer
(Swift). Read them from an ohs-libs checkout, never from a copy, as the
other vectors are (Kotlin: the `shack.spec` system property,
`kotlin/shack-ui-contracts/.../contracts/Vectors.kt`; Swift: the checkout's
`spec/`, `swift/Tests/ShackFormatTests/FormatVectorsTests.swift`).

`tools/gen-ui2-vectors.py` writes every file here except `fixtures/`, and
stops if `shack_ui` disagrees with an answer written by hand in it. Do not
edit the JSON by hand: change the generator, run it, review the diff.
Each file is `{"description", "version": 1, ..., "cases": [...]}`, one
case per line, and every case has an `id` (use it as the test's name) and
sometimes a `note` (why the case exists, not part of the check).

| File | Cases | A port passes when |
|---|---|---|
| `documents.json` | screen documents, each `document` (inline) or `fixture` (a path under `fixtures/`), with `baseKind` (`app`, or `platform` for the launcher) | its validator reports exactly `problems`, in that order: `[{code, where}]` with `where` a JSON pointer into the document; `[]` is valid. `schema` is whether `spec/schemas/ui2.schema.json` accepts it, for a port that has a schema engine (the renderers do not need one) |
| `typed.json` | typed values (§2.1) | `ok` says whether the value is well formed |
| `paths.json` | paths and URLs (§9, §11) with their `kind`: `path`, `image` or `link` | it refuses with `problem` (`bad_path`, `absolute_url`) or accepts (`null`) |
| `ops.json` | a starting document (`"base"`: the file's own `base`; or `fixture`; or inline) and `steps`, each `{"ops": [...]}` or `{"frame": {...}}` | after every step in turn, its state equals `state` (below) |
| `aircraft-stream.json` | the whole Aircraft minute: `fixtures/streams/radio/aircraft.jsonl` on `fixtures/screens/radio/aircraft.json` | after the frame on 0-based line `line` of the stream, its state equals the checkpoint's `state` |
| `fixtures/` | the contract's worked examples, copied (see its README) | every document there is in `documents.json` as valid |

## The problem codes

`bad_version`, `unknown_part`, `duplicate_id`, `bad_reference`, `bad_path`,
`absolute_url`, `cross_app_open`, `bad_action`, `bad_placeholder`,
`key_route`, `transmit_action`, `transmit_part_action`,
`template_binding`, `bad_typed`. What each means is the rule of the same
name in `python/shack_ui/src/shack_ui/check.py`, one small function each.
The order is the document-level rules first (version, selections, live and
poll paths, backdrop, holds, keys, items' own actions, root), then every
part in document order, depth first (children, a tabs part's pages,
`child`, `template`, `empty`, `fallback`, `overlay`), then duplicate ids.
A renderer that only needs a yes or no may compare `problems == []`, but
the order is pinned so that ports which report problems agree.

## The state `ops.json` compares

A renderer holds parts flat by id, with collections and chart points
beside them (SCREENS.md §4.3, item 3). The vectors write that as:

```json
{"parts": {"status": {"type": "text", "id": "status", "value": "Ready"}, ...},
 "collections": {"c": {"items": {"a": {...}}, "order": ["a", "b"]}},
 "drawn": {"c": ["a", "b"]},
 "charts": {"snr": [[[0, 1]], []]},
 "toasts": [{"text": "Hi", "tone": "warn", "s": 6}],
 "reload": false}
```

- `parts`: every part with an `id`, outside templates, with its own
  properties: `children`, `child`, `template`, `empty`, `fallback` and
  `overlay` left out when they hold a part (a spectrum's `overlay: true`
  is a property and stays), and a `tabs` part's `tabs` without each `child`.
- `collections`: items and `order` as held; a collection's `filter` is
  not state.
- `drawn`: per collection, the keys in `order` that have an item, then
  every other item sorted by key (code point order). Not JSON object
  order: Swift's parser keeps none, and nothing a box sends can pin it.
- `charts`: per chart id, the points of each mark by index.
- `toasts`: every toast op, in order; `reload`: whether a reload op came.

How ops apply (§4.2) is `python/shack_ui/src/shack_ui/ops.py`, and these
are the points a port gets wrong:

- an op naming an unknown part, collection, key, chart mark or op is
  ignored, and so is anything in `ops` that is not an object;
- `set` and `up` merge, and a `null` value deletes the property or field;
- `rm` takes the key out of `order` too;
- `push` and `append` keep the last `max` when `max` is given;
- `replace` drops every id'd part under the old one and indexes the new
  one, which keeps the id it replaces;
- a **`snap` frame is applied to the document's own state**, not on top of
  what was held: after a reconnect, what went while the socket was down
  must not survive. `hello` and `ping` change nothing.
