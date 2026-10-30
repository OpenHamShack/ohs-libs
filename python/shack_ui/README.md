# shack-ui

Server-driven screens, **ui/2** (openhamshack `docs/contract/SCREENS.md`).
Replaces ui/1 (CLIENT-UI-PLAN.md §5), which no client ever drew.

| Module | What |
|---|---|
| `shack_ui.check` | the validator: `check(doc)` returns every `Problem` (code, where, message) in document order; the rules every renderer also enforces |
| `shack_ui.ops` | the reference op applier: `state_from_document`, `apply_ops`, `apply_frame`, `drawn_keys`, `public_state` |
