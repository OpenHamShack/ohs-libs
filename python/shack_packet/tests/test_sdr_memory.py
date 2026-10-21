"""keep_heap: with glibc's heap top padded, the channeliser's FFTs stop
faulting pages back in every block (sdr/memory.py has the measurements)."""

from __future__ import annotations

import subprocess
import sys

import pytest

from shack_packet.sdr.memory import keep_heap

# The trap as it was found: a large array allocated and freed first (which
# leaves glibc's mmap threshold low), then a second of 2.4 MS/s IQ.
PROBE = r"""
import resource, sys, numpy as np
if sys.argv[1] == "kept":
    from shack_packet.sdr.memory import keep_heap
    assert keep_heap()
from shack_packet.sdr.channeliser import Channeliser
from shack_packet.sdr.plan import default_plan
plan = default_plan()
big = ((np.random.randn(12_000_000) + 1j * np.random.randn(12_000_000)) * 0.01).astype(np.complex64)
r = big[:4_800_000].copy()
del big
ch = Channeliser(2.4e6, plan.centre_for(2.4e6), list(plan))
ch.feed(r[:120_000])
before = resource.getrusage(resource.RUSAGE_SELF).ru_minflt
for i in range(120_000, len(r), 120_000):
    ch.feed(r[i:i + 120_000])
print(resource.getrusage(resource.RUSAGE_SELF).ru_minflt - before)
"""


def faults(mode: str) -> int:
    out = subprocess.run([sys.executable, "-c", PROBE, mode], capture_output=True, text=True, check=True)
    return int(out.stdout.strip())


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="glibc only")
def test_a_padded_heap_takes_the_page_faults_away():
    plain = faults("plain")
    if plain < 5_000:
        pytest.skip(f"this glibc does not show the trap ({plain} faults in two seconds of IQ)")
    assert faults("kept") < plain / 20


def test_keep_heap_is_done_once_and_says_whether_it_took():
    first = keep_heap()
    assert keep_heap() is first
    assert isinstance(first, bool)
