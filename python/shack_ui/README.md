# shack-ui

Server-driven screens, **ui/1** (CLIENT-UI-PLAN.md §5). An app builds a
screen document from the closed component vocabulary; the OpenHamShack
client (Compose, SwiftUI) draws it natively, so a new panel or setting
ships with the box app and needs no client release.

| Module | What |
|---|---|
| `shack_ui.build` | `Document`, `node(c, *children, action=, fallback=, **props)`, `native(name, v=1, fallback=, **props)`, `Bind(path, format)`, `Action.post/open/back/local/native/confirm`. `Document.to_json()` returns the validated document. |
| `shack_ui.validate` | `validate(doc)`: named checks, then `ui.schema.json`. Raises `UIError` with `code` `transmit_action`, `transmit_component_action`, `unknown_component`, `unknown_native`, `unknown_action` or `schema`. |

## Transmit

No generic action may transmit (§5.6). `Action.post(..., level="transmit")`
raises `TransmitActionError` where it is written, `native("ptt",
action=...)` does too, and `validate` refuses either in a document built
by hand. Keying is only the native `ptt`/`enableTx` components, whose
lease and time-outs are client code; the server only decides whether to
place them. The schema says the same, so a client refuses such a
document without code of its own.

`src/shack_ui/ui.schema.json` is a copy of `spec/schemas/ui.schema.json`
for the wheel; `tools/gen-client-vectors.py` refreshes it and a test
fails when they differ.
