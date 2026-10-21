# shack-bandplan

The OpenHamShack band-plan and licence dataset (`data/bandplan/` in
ohs-libs, carried inside the wheel) and the check every app makes before
keying: `shack_bandplan.check(context(profile), Signal(...))` returns a
verdict with `allowed`, the refusal and warnings as whole sentences, the
power limit, and the `dataVersion` it was judged against.

Design: `~/openhamshack/docs/contract/BANDPLAN-DATA.md`. Stdlib only,
Python 3.11+. GPL-3.0-or-later.
