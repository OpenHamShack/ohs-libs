# PSK fix status (branch fix/psk-tail-and-step, stopped 23/10/2026)

Stopped at Brian's request so it can be picked up later. All work is
committed; nothing is half-written.

## Done (three commits on 30765af)
- c7edbd5 - the tail: the squelch closing releases the trellis's pending
  characters at once; a last character whose own bits are plainly the
  over's prints with no idle after it. Last character 0.4-0.7 s after the
  carrier (was: needed 0.13 s idle, then ~1.4 s).
- 61dc2e0 - a carrier step mid-over: step detector beside the Costas loop,
  measured and placed on the look-back, look-back replayed with the AFC
  stepped there. Only on overs >= 12 dB Es/N0.
- 6f31c90 - Python Host tests for both, README PSK section.

## Tests
- Rust `cargo test --release` (whole crate): all pass. New:
  an_over_that_stops_dead_..., an_over_cut_off_mid_character_...,
  a_frequency_step_mid_over_costs_no_character (45/45; 36/45 lost
  characters before), varicode pending_is_what_push_has_still_to_decide.
- psk.py sweep identical to before at every point; noise-only and noise
  after strong overs: 0 characters; CPU unchanged.
- pytests/test_psk.py: 6 pass (the 2 new ones fail on the old build).
- NB branch feature/radio-psk-sstv, run against a dev build of this branch:
  test_tx_drain psk31 no-idle-tail and test_playback psk31 both pass (the
  drain test fails on the old build).
- Wheels were built once (~/build/ohs-dist/shack-sdr-m2b, encumbered check
  clean, smoke tests ok) before the stop request; not vendored anywhere.

## Not done
- tools/test.sh not run on this branch.
- Not merged to hub main; worktree ~/wt/sdr-psk-fix left in place.
- Known limits: steps under 12 dB Es/N0 still left to the loop; a step
  very near half the baud at PSK31 (14.5 Hz) occasionally mis-placed.

## Next step
Run tools/test.sh in the worktree; if green, rebuild the wheels (hub main
may have moved), merge/fast-forward hub main, and hand the SHA256SUMS to
Radio to vendor (then its two red tests on feature/radio-psk-sstv go green).
