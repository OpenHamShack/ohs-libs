"""A serial port, opened exclusively and read without a poll loop.

Deliberately knows nothing about CI-V, or about Icom, or about radios. The
identification pass in `usbprobe.py` has to open a port belonging to a device
nobody here has ever spoken to, and the transport in `icomusb.py` has to open
one belonging to a radio - the two want the same thirty lines of termios and
should not each have their own.

**No pyserial.** It would be the module's first dependency that is not in the
standard library, on a Raspberry Pi appliance that has to install offline, to
replace about a hundred lines of `termios` calls that have not changed since
the 1980s. `civ.py` has the same argument written into it about the protocol;
this is the same argument about the wire.

**Exclusivity is taken twice, and both are needed.** `module.yaml` says why
this module exists: "the IC-9700 accepts one network client at a time, so
without something in the middle, running WSJT-X means not running anything
else." A tty enforces nothing at all - two processes may open one and each gets
a random half of the bytes, which is worse than a refusal because neither one
fails. So: `flock`, which is what serial software conventionally respects and
what a second copy of this module would see; and `TIOCEXCL`, which is the
kernel refusing the next `open()` outright and is the one that holds across a
container boundary. Either alone leaves a real way for two programs to share
one radio and get half a conversation each.

**DTR and RTS are dropped the moment the port is open**, before anything is
written, and that is a safety decision rather than a tidiness one. On an Icom
the second USB serial port is the SEND/keying port: RTS or DTR asserted means
*transmit*. Opening a tty raises both lines by default, so a probe that walked
over the keying port of a radio connected to a real antenna would key it. The
lines are lowered immediately; the glitch between `open()` and the ioctl is
some microseconds, against an indefinite carrier if it is left alone.

**Where there is no termios (Windows), pyserial does the same job**, if it is
installed - the desktop apps' optional extra, never the appliance's. It is
opened the one way that never raises either line: the lines are set low on
the unopened object, and pyserial hands them to the driver *with* the open
(Windows' DCB carries RTS_CONTROL_DISABLE and DTR_CONTROL_DISABLE into
SetCommState), so there is not even the POSIX glitch. Yaesu's own FTX-1 CAT
manual (p. 2) is the reason this matters beyond Icom: "When using the USB
cable for TX control, the transceiver may switch to the transmit mode when
the computer is started" - a port opened with the lines up keys a radio
whose RPTT SELECT names that line. Without pyserial a Windows port is
refused in words, as it always was.

**A line is raised only by `set_line`**, which exists for one caller: a PTT
driver whose operator has said this line *is* the radio's PTT (an FTX-1's
Standard COM port with RPTT SELECT = RTS, a sound-card interface keyed by
DTR). Nothing in this module calls it.
"""

from __future__ import annotations

import errno
import logging
import os
import select
import struct
import time

# POSIX only. The Icom package imports this module whatever the transport,
# so on Windows (OpenHamShack Icom's desktop form) a top-level import of
# either took the whole library down with it - the network link included,
# which needs neither. Without them a port cannot be opened, and open() says
# so; nothing else here is reachable without an open port.
try:
    import fcntl
    import termios
except ImportError:                     # pragma: no cover - Windows
    fcntl = termios = None

from .i18n import _

log = logging.getLogger("shack_rig.serialio")

#: The line speeds a radio might be set to, and the termios constant for each.
#: Anything not in here cannot be asked for without the custom-divisor ioctl,
#: which no amateur radio needs.
BAUDS: dict[int, int] = {
    1200: termios.B1200, 2400: termios.B2400, 4800: termios.B4800,
    9600: termios.B9600, 19200: termios.B19200, 38400: termios.B38400,
    57600: termios.B57600, 115200: termios.B115200,
} if termios is not None else {b: b for b in (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200)}

#: Lowered on open. See the module docstring: on an Icom these are transmit.
_MODEM_TX_LINES = termios.TIOCM_DTR | termios.TIOCM_RTS if termios is not None else 0

#: The two lines `set_line` may drive, by name, as TIOCM bits.
_LINES = {"rts": termios.TIOCM_RTS, "dtr": termios.TIOCM_DTR} if termios is not None else {"rts": 0, "dtr": 0}


def _pyserial():
    """The pyserial module, or None. Asked only where termios is missing:
    the appliance and every POSIX desktop use the termios path, so pyserial
    is never a second way to open one port there."""
    try:
        import serial                   # noqa: PLC0415 - optional, Windows only
    except ImportError:
        return None
    return serial if getattr(serial, "Serial", None) is not None else None


class SerialError(Exception):
    """A port that could not be opened, with which of the reasons it was.

    The reason is the whole value of this class. `docs/TRANSPORTS.md` §7.4
    names the single least useful string in the module - "Not connected",
    shown identically for a port that would not open, a port that opened and
    said nothing, and a port that answered something that was not CI-V. On an
    appliance three hundred miles away with nobody on site, the difference
    between those three is the difference between a phone call and a visit.
    """

    #: A short word for the state payload, so the page can group causes.
    kind = "error"


class PortMissing(SerialError):
    """No such device. Usually the radio is switched off, or was unplugged."""

    kind = "missing"


class PortDenied(SerialError):
    """The device is there and we may not open it.

    Inside a container this is almost always the same fault: the node is
    `root:dialout 0660`, the service account is in `dialout`, and rootless
    Podman dropped the supplementary groups on the way in. `quadlet.py` emits
    `--group-add keep-groups` for exactly this, so the usual cause is a device
    assigned by hand into a unit written before that, or a host where the
    account was never joined to the group.
    """

    kind = "denied"


class PortBusy(SerialError):
    """Another program holds this port.

    Reported apart from every other failure because it is the one that is not
    a fault with the radio at all, and because the natural reading of a radio
    that will not answer - power, cable, baud rate - sends somebody looking in
    the wrong place entirely.
    """

    kind = "busy"


class SerialPort:
    """One open tty: raw, at a chosen speed, held exclusively.

    Synchronous on purpose. The identification pass runs it from a worker
    thread (see `usbprobe.probe`) and the transport drives it from the event
    loop through `fileno()`, so neither an async API nor a reader task belongs
    here - and a class that is only file descriptors and ioctls can be tested
    against a pty with no event loop at all.
    """

    def __init__(self, device: str, baud: int, stop_bits: int = 1):
        if baud not in BAUDS:
            raise SerialError(_("{baud} is not a speed this can set. Try one of {speeds}").format(
                baud=baud, speeds=", ".join(str(b) for b in sorted(BAUDS))))
        if stop_bits not in (1, 2):
            raise SerialError(_("{n} stop bits is not something a serial port has; use 1 or 2").format(
                n=stop_bits))
        self.device = device
        self.baud = baud
        #: 1 for CI-V and the FTX-1 (its menu ships at 1 bit, CAT manual
        #: p. 3); 2 where a radio's CAT wants it (hamlib opens the FT-991
        #: at 8N2). Sending two to a radio set for one is harmless - the
        #: second is idle line - which is why a model table may choose 2.
        self.stop_bits = stop_bits
        self.fd: int | None = None
        self._saved: list | None = None
        #: The pyserial object where there is no termios (module docstring).
        self._ser = None
        #: What this object last set each line to, for a PTT driver's
        #: diagnostics; both False from the moment of opening.
        self.line_state = {"rts": False, "dtr": False}

    # -- opening and closing ----------------------------------------------

    def open(self) -> None:
        """Open, take both locks, lower the transmit lines, set raw termios.

        In that order, and the order is the point. The lock is taken before
        the line settings are touched, so a port somebody else is using is
        left exactly as they had it rather than reconfigured underneath them
        and then handed back.
        """
        if termios is None:
            self._open_pyserial()
            return
        try:
            fd = os.open(self.device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except FileNotFoundError:
            raise PortMissing(_("{device} is not there").format(device=self.device)) from None
        except PermissionError:
            raise PortDenied(_("{device} cannot be opened by this user").format(device=self.device)) from None
        except OSError as e:
            if e.errno in (errno.EBUSY, errno.EACCES):
                raise PortBusy(_("{device} is held by something else").format(device=self.device)) from None
            raise SerialError(f"{self.device}: {e.strerror or e}") from None

        try:
            # TIOCEXCL first: it is the one that refuses the *next* opener, so
            # taking it early narrows the window in which a second program can
            # get in behind us.
            try:
                fcntl.ioctl(fd, termios.TIOCEXCL)
            except OSError:
                # A pty has no exclusive mode. The loopback tests live on one,
                # and refusing to run there would mean the only way to exercise
                # this file is with a radio plugged in.
                pass
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PortBusy(_("another program has {device} open").format(device=self.device)) from None
            self._lower_tx_lines(fd)
            self._saved = termios.tcgetattr(fd)
            self._configure(fd)
        except Exception:
            os.close(fd)
            raise
        self.fd = fd

    def _open_pyserial(self) -> None:
        """The Windows path: pyserial, opened with both lines already low.

        `rts` and `dtr` are set on the unopened object, which pyserial
        applies as part of opening rather than after it - the order is the
        point, and tests/test_serialio.py checks it against a stand-in
        module that records what happened when.
        """
        serial = _pyserial()
        if serial is None:
            raise SerialError(_("a radio's USB serial port cannot be opened on this system; "
                                "connect to it over the network instead"))
        ser = serial.Serial()
        ser.port = self.device
        ser.baudrate = self.baud
        ser.bytesize = 8
        ser.parity = "N"
        ser.stopbits = self.stop_bits
        ser.xonxoff = False
        ser.rtscts = False
        ser.dsrdtr = False
        ser.rts = False
        ser.dtr = False
        ser.timeout = 0
        # A write is made on the rig's event loop (usb.py's writer), so a
        # write that blocks blocks every CI-V answer and the next unkey with
        # it. A CI-V frame is a dozen bytes, a few milliseconds even at
        # 4800 baud; a quarter of a second only expires on a port that has
        # stopped taking bytes, where waiting the full second this was
        # gained nothing but a second of a stuck loop per frame.
        ser.write_timeout = 0.25
        if hasattr(ser, "exclusive"):
            # POSIX pyserial's flock; Windows opens COM ports exclusively
            # anyway, and pyserial there has no such attribute.
            ser.exclusive = True
        try:
            ser.open()
        except Exception as e:                      # noqa: BLE001 - pyserial's SerialException and OSErrors
            text = str(e).lower()
            if "denied" in text or "busy" in text or "in use" in text:
                raise PortBusy(_("{device} is held by something else").format(device=self.device)) from None
            if "not find" in text or "no such" in text or "cannot find" in text:
                raise PortMissing(_("{device} is not there").format(device=self.device)) from None
            raise SerialError(f"{self.device}: {e}") from None
        self._ser = ser
        self.fd = -1

    @staticmethod
    def _lower_tx_lines(fd: int) -> None:
        """Drop DTR and RTS. On an Icom these are the keying port's PTT."""
        try:
            fcntl.ioctl(fd, termios.TIOCMBIC, struct.pack("I", _MODEM_TX_LINES))
        except OSError:
            # A pty has no modem lines. Nothing to lower and nothing at risk.
            pass

    def _configure(self, fd: int) -> None:
        """8N1, raw, no flow control, at this port's speed.

        No flow control is not a default being accepted: CI-V is three wires
        and a CP210x with RTS/CTS asked for would wait for a handshake the
        radio never makes, which presents as a port that opens and swallows
        everything written to it.
        """
        iflag, oflag, cflag, lflag, _, _, cc = termios.tcgetattr(fd)
        iflag = 0
        oflag = 0
        lflag = 0
        cflag |= termios.CLOCAL | termios.CREAD
        cflag &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB)
        cflag |= termios.CS8
        if self.stop_bits == 2:
            cflag |= termios.CSTOPB
        if hasattr(termios, "CRTSCTS"):
            cflag &= ~termios.CRTSCTS
        cc = list(cc)
        # A read returns whatever is there and never waits. The waiting is done
        # by select() or by the event loop, which is where a timeout can be
        # shared with everything else rather than buried in the driver.
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        speed = BAUDS[self.baud]
        termios.tcsetattr(fd, termios.TCSANOW,
                          [iflag, oflag, cflag, lflag, speed, speed, cc])
        termios.tcflush(fd, termios.TCIOFLUSH)

    def close(self) -> None:
        """Release everything, and be safe to call on a port never opened.

        The locks go with the file descriptor, so the close is what frees the
        port - but the line settings are put back first, because leaving a
        radio's port at a speed we chose is a change to somebody else's device
        that outlives this process.
        """
        fd, self.fd = self.fd, None
        if fd is None:
            return
        ser, self._ser = self._ser, None
        if ser is not None:
            # Lines down first, explicitly: a PTT line left up by a caller
            # that died is a keyed radio, and the close alone is the
            # driver's business on Windows.
            for name in ("rts", "dtr"):
                try:
                    setattr(ser, name, False)
                except Exception:               # noqa: BLE001 - closing regardless
                    pass
            self.line_state = {"rts": False, "dtr": False}
            try:
                ser.close()
            except Exception:                   # noqa: BLE001
                pass
            return
        # The same for a termios port: a line a PTT driver raised goes down
        # before anything else, because the restored settings below could
        # carry HUPCL off and leave it up after we have gone.
        self._lower_tx_lines(fd)
        self.line_state = {"rts": False, "dtr": False}
        try:
            if self._saved is not None:
                termios.tcsetattr(fd, termios.TCSANOW, self._saved)
        except OSError:
            pass
        try:
            # Given back explicitly rather than left to the close. On a USB
            # tty the close does clear it, because ours is the last descriptor
            # open - but "the last descriptor" is an assumption about the rest
            # of the host, and a port left in exclusive mode is one nothing can
            # open again until it is unplugged.
            fcntl.ioctl(fd, termios.TIOCNXCL)
        except OSError:
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    # -- bytes -------------------------------------------------------------

    def fileno(self) -> int:
        if self.fd is None:
            raise SerialError(_("{device} is not open").format(device=self.device))
        if self._ser is not None:
            # A Windows COM port is not a descriptor select() or an event
            # loop can wait on; read_wait() is the portable way to wait.
            raise SerialError(_("{device} cannot be watched by the event loop on this system").format(
                device=self.device))
        return self.fd

    # -- the modem lines, for a PTT driver only --------------------------

    def set_line(self, name: str, on: bool) -> bool:
        """Raise or lower RTS or DTR. True if the port took it.

        Only for a PTT driver whose operator named this line as the radio's
        PTT (module docstring). False on a port without modem lines (a
        pty), which a driver reports rather than believes: a line that
        could not be raised did not key anything, and one that could not be
        lowered was never raised by us.
        """
        if name not in _LINES:
            raise SerialError(_("{name} is not RTS or DTR").format(name=repr(name)))
        if self.fd is None:
            return False
        if self._ser is not None:
            try:
                setattr(self._ser, name, bool(on))
            except Exception as e:              # noqa: BLE001 - pyserial raises its own
                log.warning("%s: setting %s failed: %s", self.device, name.upper(), e)
                return False
            self.line_state[name] = bool(on)
            return True
        try:
            fcntl.ioctl(self.fd, termios.TIOCMBIS if on else termios.TIOCMBIC, struct.pack("I", _LINES[name]))
        except OSError as e:
            log.debug("%s: setting %s failed: %s", self.device, name.upper(), e.strerror or e)
            return False
        self.line_state[name] = bool(on)
        return True

    def read_wait(self, timeout: float) -> bytes:
        """Whatever arrives within `timeout` seconds; returns at the first
        bytes. The portable read for a caller with a thread of its own (the
        Yaesu CAT reader), on either path."""
        if self.fd is None:
            return b""
        if self._ser is not None:
            ser = self._ser
            end = time.monotonic() + timeout
            while True:
                waiting = getattr(ser, "in_waiting", 0)
                if waiting:
                    return bytes(ser.read(waiting))
                left = end - time.monotonic()
                if left <= 0:
                    return b""
                time.sleep(min(0.005, left))
        ready, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        if not ready:
            return b""
        return self.read()

    def write(self, data: bytes) -> bool:
        """Write, and say whether all of it went.

        A CI-V frame is eleven bytes and the kernel's buffer is thousands, so
        a short write here means the port has gone rather than that it is
        busy - which is why this returns a boolean instead of looping.
        """
        if self.fd is None:
            return False
        if self._ser is not None:
            try:
                return self._ser.write(data) == len(data)
            except Exception as e:              # noqa: BLE001 - pyserial's own exceptions
                log.warning("%s: write failed: %s", self.device, e)
                return False
        try:
            return os.write(self.fd, data) == len(data)
        except (BlockingIOError, InterruptedError):
            return False
        except OSError as e:
            log.warning("%s: write failed: %s", self.device, e.strerror or e)
            return False

    def read(self) -> bytes:
        """Whatever has arrived, possibly nothing. Never blocks.

        Empty bytes mean "nothing yet" and are the normal case; the caller is
        told the port has gone by an OSError, which is what a radio being
        unplugged mid-session raises.
        """
        if self.fd is None:
            return b""
        if self._ser is not None:
            waiting = getattr(self._ser, "in_waiting", 0)
            return bytes(self._ser.read(waiting)) if waiting else b""
        try:
            return os.read(self.fd, 4096)
        except (BlockingIOError, InterruptedError):
            return b""

    def exchange(self, request: bytes, wait: float) -> bytes:
        """Send something and gather what comes back inside `wait` seconds.

        Gathers for the whole window rather than returning on the first byte.
        A serial reply arrives a byte at a time - eleven bytes at 38400 take
        under three milliseconds, but the radio's own turnaround is tens - and
        a caller that stopped at the first read would be handed the first two
        bytes of a frame and call it unreadable.
        """
        if self._ser is not None:
            try:
                self._ser.reset_input_buffer()
            except Exception:                   # noqa: BLE001 - a stand-in may not have it
                pass
        else:
            termios.tcflush(self.fileno(), termios.TCIFLUSH)
        if not self.write(request):
            return b""
        got = b""
        end = time.monotonic() + wait
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return got
            chunk = self.read_wait(left)
            if chunk:
                got += chunk
            elif self._ser is None:
                return got
