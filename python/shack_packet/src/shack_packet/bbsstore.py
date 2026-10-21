"""The BBS's mail store: personal mail and bulletins, who has had what,
and what the BBS knows of each station. SQLite, one file, so the
controller's BBS keeps its mail across restarts and updates and a backup
of the app's volume carries it.

    store = BbsStore("/data/bbs.db", home="GM4BRI")
    msg = store.post("P", "G0ABC", "GM4BRI", "GM4BRI", "Sked", "2 m at 2000?", now=t)
    store.listing(call="GM4BRI", ...)

**Numbers are never reused** (AUTOINCREMENT): a station that wrote down
"read 1234" never reads somebody else's message by it later.

**Every BID ever stored is remembered** (`bids`) for BID_KEEP_DAYS after
its message is purged, so a forwarding partner offering it again is
answered "have it" (FORWARDING.md §6) rather than delivering it twice.

**Expiry** is set when a message is stored: bulletins `bulletin_days`,
personal mail `personal_days`, and personal mail that has been read (or
collected by its addressee) `read_days` after that. `sweep()` deletes what
has expired and what was killed; the app calls it now and then.

**The schema has a version** (`PRAGMA user_version`). Opening runs each
numbered migration the file has not had; a file newer than this code is
refused (`StoreTooNew`) rather than used, because an older BBS writing to
a newer schema could lose what the newer one keeps.

Thread-safe: one connection, used under one lock, as the BBS's engine
thread and the sysop page's HTTP threads both use it.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

DAY = 86_400.0
BID_KEEP_DAYS = 365
SCHEMA = 1

_MIGRATIONS = {
    1: """
    CREATE TABLE messages(
        number INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL,
        sender TEXT NOT NULL,
        recipient TEXT NOT NULL,
        at TEXT NOT NULL DEFAULT '',
        subject TEXT NOT NULL,
        body TEXT NOT NULL,
        bid TEXT NOT NULL UNIQUE,
        size INTEGER NOT NULL,
        created REAL NOT NULL,
        expires REAL NOT NULL,
        state TEXT NOT NULL DEFAULT 'new',
        origin TEXT NOT NULL DEFAULT 'local',
        read_at REAL);
    CREATE INDEX messages_recipient ON messages(recipient);
    CREATE TABLE deliveries(
        number INTEGER NOT NULL,
        peer TEXT NOT NULL,
        how TEXT NOT NULL,
        at REAL NOT NULL,
        PRIMARY KEY(number, peer));
    CREATE TABLE users(
        call TEXT PRIMARY KEY,
        name TEXT NOT NULL DEFAULT '',
        home TEXT NOT NULL DEFAULT '',
        qth TEXT NOT NULL DEFAULT '',
        first_seen REAL,
        last_seen REAL,
        connects INTEGER NOT NULL DEFAULT 0,
        barred INTEGER NOT NULL DEFAULT 0,
        last_listed INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE bids(bid TEXT PRIMARY KEY, seen REAL NOT NULL);
    """,
}


class StoreTooNew(Exception):
    """The database was written by a newer BBS than this one."""


@dataclass
class Message:
    number: int
    type: str
    sender: str
    recipient: str
    at: str
    subject: str
    body: str
    bid: str
    size: int
    created: float
    expires: float
    state: str
    origin: str
    read_at: float | None

    @property
    def personal(self) -> bool:
        return self.type == "P"

    def public(self, with_body: bool = False) -> dict:
        out = {"number": self.number, "type": self.type, "from": self.sender, "to": self.recipient,
               "at": self.at, "subject": self.subject, "bid": self.bid, "size": self.size,
               "created": self.created, "expires": self.expires, "state": self.state, "origin": self.origin}
        if with_body:
            out["body"] = self.body
        return out


@dataclass
class User:
    call: str
    name: str = ""
    home: str = ""
    qth: str = ""
    first_seen: float | None = None
    last_seen: float | None = None
    connects: int = 0
    barred: bool = False
    last_listed: int = 0

    def public(self) -> dict:
        return {"call": self.call, "name": self.name, "home": self.home, "qth": self.qth,
                "firstSeen": self.first_seen, "lastSeen": self.last_seen, "connects": self.connects,
                "barred": self.barred}


@dataclass
class Retention:
    bulletin_days: float = 30.0
    personal_days: float = 90.0
    read_days: float = 14.0


def base_call(call: str) -> str:
    """G0ABC-9 is G0ABC: mail belongs to the operator, not the SSID."""
    return call.upper().split("-")[0].strip()


def at_home(at: str) -> str:
    """The BBS part of an @ address: `GB7XXX.#79.GBR.EURO` is GB7XXX."""
    return at.upper().split(".")[0].strip()


class BbsStore:
    def __init__(self, path: str | Path = ":memory:", *, home: str, retention: Retention | None = None,
                 clock=time.time):
        self.home = base_call(home)
        self.retention = retention or Retention()
        self.clock = clock
        self._lock = threading.RLock()
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        if str(path) != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _migrate(self) -> None:
        with self._lock:
            have = self._db.execute("PRAGMA user_version").fetchone()[0]
            if have > SCHEMA:
                raise StoreTooNew(f"the mail store is schema {have}; this BBS knows up to {SCHEMA}")
            for version in range(have + 1, SCHEMA + 1):
                self._db.execute("BEGIN")
                try:
                    # Statement by statement inside one transaction:
                    # executescript() would commit on its own first.
                    for statement in _MIGRATIONS[version].split(";"):
                        if statement.strip():
                            self._db.execute(statement)
                    self._db.execute(f"PRAGMA user_version = {version}")
                    self._db.execute("COMMIT")
                except Exception:
                    self._db.execute("ROLLBACK")
                    raise

    # ------------------------------------------------------------- messages
    @staticmethod
    def _msg(row) -> Message:
        return Message(**{k: row[k] for k in row.keys()})

    def next_bid(self) -> str:
        """A BID for a message made here: `<n>_<HOME>`, at most 12."""
        with self._lock:
            row = self._db.execute("SELECT seq FROM sqlite_sequence WHERE name='messages'").fetchone()
            n = (row[0] if row else 0) + 1
        bid = f"{n}_{self.home}"
        return bid[:12]

    def has_bid(self, bid: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM bids WHERE bid=?", (bid.upper(),)).fetchone() is not None

    def post(self, type_: str, sender: str, recipient: str, at: str, subject: str, body: str, *,
             bid: str | None = None, origin: str = "local", now: float | None = None) -> Message | None:
        """Store a message; None if its BID is already known."""
        now = self.clock() if now is None else now
        type_ = type_.upper()
        days = self.retention.bulletin_days if type_ == "B" else self.retention.personal_days
        with self._lock:
            bid = (bid or self.next_bid()).upper()
            if self.has_bid(bid):
                return None
            self._db.execute("BEGIN")
            try:
                cur = self._db.execute(
                    "INSERT INTO messages(type, sender, recipient, at, subject, body, bid, size, created, expires,"
                    " origin) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (type_, base_call(sender), recipient.upper(), at.upper(), subject, body, bid,
                     len(body.encode("utf-8")), now, now + days * DAY, origin))
                self._db.execute("INSERT OR REPLACE INTO bids(bid, seen) VALUES (?, ?)", (bid, now))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
            return self.get(cur.lastrowid)

    def get(self, number: int) -> Message | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM messages WHERE number=? AND state != 'killed'",
                                   (number,)).fetchone()
        return self._msg(row) if row else None

    def messages(self, *, type_: str | None = None, to: str | None = None, sender: str | None = None,
                 at: str | None = None, since: int = 0, last: int | None = None,
                 states=("new", "read")) -> list[Message]:
        """Messages, oldest first, filtered; `last` keeps the newest n."""
        where, args = ["number > ?"], [since]
        if states:
            where.append("state IN (%s)" % ",".join("?" * len(states)))
            args += list(states)
        if type_:
            where.append("type = ?")
            args.append(type_.upper())
        if to:
            where.append("recipient = ?")
            args.append(to.upper())
        if sender:
            where.append("sender = ?")
            args.append(base_call(sender))
        if at:
            where.append("(at = ? OR at LIKE ?)")
            args += [at.upper(), f"%.{at.upper()}%"]
        sql = "SELECT * FROM messages WHERE " + " AND ".join(where) + " ORDER BY number"
        with self._lock:
            rows = [self._msg(r) for r in self._db.execute(sql, args)]
        return rows[-last:] if last else rows

    def visible_to(self, call: str, msg: Message) -> bool:
        """Bulletins are everybody's; personal mail is its sender's and its
        addressee's (the sysop reads everything through the page)."""
        call = base_call(call)
        return msg.type == "B" or msg.recipient == call or msg.sender == call

    def mark_read(self, number: int, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        with self._lock:
            self._db.execute(
                "UPDATE messages SET state='read', read_at=?, expires=MIN(expires, ?)"
                " WHERE number=? AND type='P' AND state='new'",
                (now, now + self.retention.read_days * DAY, number))

    def kill(self, number: int) -> bool:
        with self._lock:
            cur = self._db.execute("UPDATE messages SET state='killed' WHERE number=? AND state != 'killed'",
                                   (number,))
            return cur.rowcount > 0

    def sent_since(self, call: str, since: float, *, any_origin: bool = False) -> int:
        """Messages `call` has left here since then: typed at the BBS, or
        with `any_origin` also delivered by forwarding (a station's own
        mail arrives that way)."""
        sql = "SELECT COUNT(*) FROM messages WHERE sender=? AND created>=?"
        if not any_origin:
            sql += " AND origin='local'"
        with self._lock:
            return self._db.execute(sql, (base_call(call), since)).fetchone()[0]

    def held_for(self, call: str) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM messages WHERE recipient=? AND type='P'"
                                    " AND state IN ('new','read')", (base_call(call),)).fetchone()[0]

    def sweep(self, now: float | None = None) -> int:
        """Delete expired and killed messages; forget BIDs past their keep."""
        now = self.clock() if now is None else now
        with self._lock:
            cur = self._db.execute("DELETE FROM messages WHERE state='killed' OR expires <= ?", (now,))
            self._db.execute("DELETE FROM deliveries WHERE number NOT IN (SELECT number FROM messages)")
            self._db.execute("DELETE FROM bids WHERE seen <= ? AND bid NOT IN (SELECT bid FROM messages)",
                             (now - BID_KEEP_DAYS * DAY,))
            return cur.rowcount

    # ----------------------------------------------------------- deliveries
    def delivered_to(self, number: int, peer: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM deliveries WHERE number=? AND peer=?",
                                    (number, base_call(peer))).fetchone() is not None

    def mark_delivered(self, number: int, peer: str, how: str = "sent", *, final: bool = False,
                       now: float | None = None) -> None:
        """Record that `peer` has this message. `final`: it has reached its
        addressee (collected) or left for its destination, so it leaves the
        lists and is kept only until read mail's expiry."""
        now = self.clock() if now is None else now
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO deliveries(number, peer, how, at) VALUES (?,?,?,?)",
                             (number, base_call(peer), how, now))
            if final:
                self._db.execute("UPDATE messages SET state='forwarded', read_at=COALESCE(read_at, ?),"
                                 " expires=MIN(expires, ?) WHERE number=?",
                                 (now, now + self.retention.read_days * DAY, number))

    def deliveries(self, number: int) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute("SELECT peer, how, at FROM deliveries WHERE number=?",
                                                       (number,))]

    # ---------------------------------------------------------------- users
    def user(self, call: str) -> User | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM users WHERE call=?", (base_call(call),)).fetchone()
        if row is None:
            return None
        u = User(**{k: row[k] for k in row.keys()})
        u.barred = bool(u.barred)
        return u

    def users(self) -> list[User]:
        with self._lock:
            calls = [r[0] for r in self._db.execute("SELECT call FROM users ORDER BY last_seen DESC")]
        return [self.user(c) for c in calls]

    def is_local(self, call: str) -> bool:
        """A station the BBS knows: it has connected, or mail is kept for it."""
        return self.user(call) is not None or self.held_for(call) > 0

    def seen(self, call: str, now: float | None = None) -> User:
        """A station connected: count it and stamp it."""
        now = self.clock() if now is None else now
        call = base_call(call)
        with self._lock:
            self._db.execute("INSERT INTO users(call, first_seen, last_seen, connects) VALUES (?,?,?,1)"
                             " ON CONFLICT(call) DO UPDATE SET last_seen=excluded.last_seen,"
                             " connects=connects+1", (call, now, now))
        return self.user(call)

    def update_user(self, call: str, **fields) -> None:
        allowed = {"name", "home", "qth", "barred", "last_listed"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"not a user field: {sorted(bad)}")
        call = base_call(call)
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO users(call, first_seen, last_seen) VALUES (?, NULL, NULL)",
                             (call,))
            for k, v in fields.items():
                self._db.execute(f"UPDATE users SET {k}=? WHERE call=?", (int(v) if k == "barred" else v, call))

    def counts(self) -> dict:
        with self._lock:
            row = self._db.execute(
                "SELECT SUM(type='P' AND state IN ('new','read')), SUM(type='B' AND state IN ('new','read')),"
                " SUM(state='forwarded') FROM messages").fetchone()
            users = self._db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        return {"personal": row[0] or 0, "bulletins": row[1] or 0, "forwarded": row[2] or 0, "users": users}
