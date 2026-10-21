"""The one exception the library raises for a call it cannot honour."""

from __future__ import annotations


class PacketError(ValueError):
    """A sample rate too low for the tones, a code FX.25 does not have, a
    frame that cannot be built. A bug in the caller, for the developer, so
    its message is English like a log line (I18N.md rule 8): an app checks
    what the operator typed and says what is wrong in its own translated
    words before it gets here. A ValueError, so callers can catch either."""
