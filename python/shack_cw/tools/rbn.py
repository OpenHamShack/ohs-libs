#!/usr/bin/env python3
"""Capture the Reverse Beacon Network's live spots while the corpus records.

    python3 tools/rbn.py CALL SECONDS ~/build/cw-corpus/rbn-spots.txt

Read-only: the RBN's telnet feed (telnet.reversebeacon.net:7000) asks for a
callsign and then streams every skimmer's spots; each "DX de" line is
written with the UTC time it arrived. tools/cw_corpus.py annotate matches a
recording's keyed lines against these by frequency and time, for the call
that should appear in the decode and the speed the skimmers measured.
The archive (reversebeacon.net raw_data) has the same spots a day later,
without the arrival time.
"""
import socket
import sys
import time
from datetime import datetime, timezone

call, secs, out = sys.argv[1], float(sys.argv[2]), sys.argv[3]
s = socket.create_connection(("telnet.reversebeacon.net", 7000), timeout=30)
s.settimeout(5)
buf = b""
end = time.time() + secs
logged_in = False
with open(out, "a", encoding="utf-8") as f:
    while time.time() < end:
        try:
            d = s.recv(65536)
        except socket.timeout:
            continue
        if not d:
            break
        buf += d
        if not logged_in and b"call" in buf.lower():
            s.sendall(call.encode() + b"\r\n")
            logged_in = True
        *lines, buf = buf.split(b"\n")
        for ln in lines:
            t = ln.decode("latin-1").rstrip()
            if t.startswith("DX de"):
                f.write(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {t}\n")
        f.flush()
