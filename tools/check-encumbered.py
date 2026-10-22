#!/usr/bin/env python3
"""Fail if anything encumbered is linked into, defined in or carried by an
artefact (RADIO-SDR-PLAN §5.5, §9.2).

AAC (faad2, fdk-aac) and AMBE/IMBE (mbelib) carry patents, fdk-aac its own
licence, and SDRplay's API is proprietary and the user's own: OpenHamShack
only ever opens them at run time from the user's install. This is the
check that no build of ours links, bundles or hides one. The denylist is
data, spec/encumbered.json.

    tools/check-encumbered.py PATH...            wheels, zips, .debs, folders, single files
    tools/check-encumbered.py --dpkg-root ROOT   also: no denylisted Debian package installed in ROOT

For every file it checks the name against `files`; for every native binary
(ELF, PE or Mach-O, found by its magic, not its extension) it reads the
libraries it imports (ELF NEEDED, PE import table, Mach-O load commands)
against `libraries`, and the symbols it defines (ELF dynamic and static
symbol tables, PE exports, Mach-O external symbols) against `symbols`. The
names may appear in a binary as strings - shack_sdr's codec loader looks
them up by name - but never linked, defined or exported.

The binaries are parsed here, in Python, rather than with readelf, objdump
and otool: one Linux host checks the Windows and macOS wheels too, and the
check cannot quietly pass because a tool was missing. A binary that cannot
be parsed fails the check (exit 2): an unreadable file is not a clean one.

Exit 0 clean, 1 something found, 2 something could not be checked.
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import os
import struct
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "spec" / "encumbered.json"


class Unreadable(Exception):
    pass


def match(name: str, patterns) -> str | None:
    n = name.lower()
    return next((p for p in patterns if fnmatch.fnmatchcase(n, p.lower())), None)


# -- ELF ---------------------------------------------------------------------

def elf(data: bytes) -> tuple[list[str], list[str]]:
    """(imported libraries, defined global symbols) of an ELF file."""
    if data[4] not in (1, 2) or data[5] not in (1, 2):
        raise Unreadable("bad ELF class")
    wide, end = data[4] == 2, "<" if data[5] == 1 else ">"
    if wide:
        shoff, = struct.unpack_from(end + "Q", data, 0x28)
        shentsize, shnum = struct.unpack_from(end + "HH", data, 0x3A)
    else:
        shoff, = struct.unpack_from(end + "I", data, 0x20)
        shentsize, shnum = struct.unpack_from(end + "HH", data, 0x2E)
    secs = []
    for i in range(shnum):
        o = shoff + i * shentsize
        if wide:
            _, typ, _, _, off, size, link, _, _, entsize = struct.unpack_from(end + "IIQQQQIIQQ", data, o)
        else:
            _, typ, _, _, off, size, link, _, _, entsize = struct.unpack_from(end + "IIIIIIIIII", data, o)
        secs.append((typ, off, size, link, entsize))

    def string(sec, at):
        off = secs[sec][1] + at
        return data[off:data.index(b"\0", off)].decode("latin-1")

    needed, defined = [], []
    for typ, off, size, link, entsize in secs:
        if typ == 6:  # SHT_DYNAMIC
            step = 16 if wide else 8
            for o in range(off, off + size, step):
                tag, val = struct.unpack_from(end + ("qQ" if wide else "iI"), data, o)
                if tag == 0:
                    break
                if tag == 1:  # DT_NEEDED
                    needed.append(string(link, val))
        if typ in (2, 11):  # SHT_SYMTAB, SHT_DYNSYM
            step = entsize or (24 if wide else 16)
            for o in range(off, off + size, step):
                if wide:
                    name, info, _, shndx, _, _ = struct.unpack_from(end + "IBBHQQ", data, o)
                else:
                    name, _, _, info, _, shndx = struct.unpack_from(end + "IIIBBH", data, o)
                bind = info >> 4
                # Defined, and GLOBAL or WEAK; in the static table LOCAL too,
                # because a codec linked in with hidden visibility becomes
                # local there and is still in the binary.
                if shndx != 0 and name and (bind in (1, 2) or (typ == 2 and info & 0xF in (1, 2))):
                    defined.append(string(link, name))
    return needed, defined


# -- PE ----------------------------------------------------------------------

def pe(data: bytes) -> tuple[list[str], list[str]]:
    lfanew, = struct.unpack_from("<I", data, 0x3C)
    if data[lfanew:lfanew + 4] != b"PE\0\0":
        raise Unreadable("no PE header")
    coff = lfanew + 4
    nsec, = struct.unpack_from("<H", data, coff + 2)
    optsize, = struct.unpack_from("<H", data, coff + 16)
    opt = coff + 20
    magic, = struct.unpack_from("<H", data, opt)
    dirs = opt + (112 if magic == 0x20B else 96)
    exp_rva, _ = struct.unpack_from("<II", data, dirs)
    imp_rva, _ = struct.unpack_from("<II", data, dirs + 8)
    sections = []
    for i in range(nsec):
        o = opt + optsize + 40 * i
        vsize, va, rawsize, rawptr = struct.unpack_from("<IIII", data, o + 8)
        sections.append((va, max(vsize, rawsize), rawptr))

    def off(rva):
        for va, size, raw in sections:
            if va <= rva < va + size:
                return rva - va + raw
        raise Unreadable(f"RVA {rva:#x} outside the sections")

    def cstr(rva):
        o = off(rva)
        return data[o:data.index(b"\0", o)].decode("latin-1")

    imports, exports = [], []
    if imp_rva:
        o = off(imp_rva)
        while True:
            name_rva, = struct.unpack_from("<I", data, o + 12)
            if not name_rva:
                break
            imports.append(cstr(name_rva))
            o += 20
    if exp_rva:
        o = off(exp_rva)
        n, names = struct.unpack_from("<I", data, o + 24)[0], struct.unpack_from("<I", data, o + 32)[0]
        for i in range(n):
            exports.append(cstr(struct.unpack_from("<I", data, off(names) + 4 * i)[0]))
    return imports, exports


# -- Mach-O ------------------------------------------------------------------

MH = {b"\xcf\xfa\xed\xfe": ("<", True), b"\xce\xfa\xed\xfe": ("<", False),
      b"\xfe\xed\xfa\xcf": (">", True), b"\xfe\xed\xfa\xce": (">", False)}
FAT = (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf")


def macho(data: bytes) -> tuple[list[str], list[str]]:
    if data[:4] in FAT:
        wide = data[:4] == FAT[1]
        n, = struct.unpack_from(">I", data, 4)
        libs, syms = [], []
        for i in range(n):
            if wide:
                _, _, offset, size, _, _ = struct.unpack_from(">iiQQII", data, 8 + 32 * i)
            else:
                _, _, offset, size, _ = struct.unpack_from(">iiIII", data, 8 + 20 * i)
            a, b = macho(data[offset:offset + size])
            libs += a
            syms += b
        return libs, syms
    end, wide = MH[data[:4]]
    ncmds, = struct.unpack_from(end + "I", data, 16)
    o = 32 if wide else 28
    libs, syms = [], []
    for _ in range(ncmds):
        cmd, size = struct.unpack_from(end + "II", data, o)
        if cmd in (0xC, 0x80000018, 0x8000001F, 0x20, 0x80000023):  # LOAD, WEAK, REEXPORT, LAZY, UPWARD dylib
            noff, = struct.unpack_from(end + "I", data, o + 8)
            libs.append(data[o + noff:data.index(b"\0", o + noff)].decode("latin-1"))
        if cmd == 0x2:  # LC_SYMTAB
            symoff, nsyms, stroff, _ = struct.unpack_from(end + "IIII", data, o + 8)
            step = 16 if wide else 12
            for i in range(nsyms):
                strx, ntype = struct.unpack_from(end + "IB", data, symoff + step * i)
                if ntype & 0xE0:  # a debugging (stab) entry
                    continue
                if ntype & 0x01 and ntype & 0x0E:  # external, and defined (not N_UNDF)
                    s = data[stroff + strx:data.index(b"\0", stroff + strx)].decode("latin-1")
                    syms.append(s[1:] if s.startswith("_") else s)
        o += size
    return libs, syms


def inspect(data: bytes):
    """(kind, imports, defined) for a native binary, or None if it is not one."""
    try:
        if data[:4] == b"\x7fELF":
            return ("elf",) + elf(data)
        if data[:2] == b"MZ" and len(data) > 0x40:
            return ("pe",) + pe(data)
        if data[:4] in MH or data[:4] in FAT[:1] or data[:4] == FAT[1]:
            if data[:4] == FAT[0] and struct.unpack_from(">I", data, 4)[0] > 30:
                return None  # a Java class file shares the fat magic
            return ("macho",) + macho(data)
    except (struct.error, ValueError, IndexError, KeyError) as e:
        raise Unreadable(str(e)) from e
    return None


# -- walking -----------------------------------------------------------------

class Checker:
    def __init__(self, spec: dict):
        self.spec = spec
        self.findings: list[str] = []
        self.unreadable: list[str] = []
        self.files = self.binaries = 0

    def file(self, where: str, name: str, data: bytes) -> None:
        self.files += 1
        base = name.replace("\\", "/").rsplit("/", 1)[-1]
        p = match(base, self.spec["files"])
        if p:
            self.findings.append(f"{where}: carries {base} (matches {p})")
        if base.endswith((".whl", ".zip")):
            self.zip(where, data)
            return
        if base.endswith(".deb"):
            self.deb(where, data)
            return
        try:
            got = inspect(data)
        except Unreadable as e:
            self.unreadable.append(f"{where}: cannot be parsed ({e})")
            return
        if got is None:
            return
        self.binaries += 1
        kind, imports, defined = got
        for lib in imports:
            p = match(lib.rsplit("/", 1)[-1], self.spec["libraries"])
            if p:
                self.findings.append(f"{where}: {kind} imports {lib} (matches {p})")
        for sym in sorted(set(defined)):
            p = match(sym, self.spec["symbols"])
            if p:
                self.findings.append(f"{where}: {kind} defines {sym} (matches {p})")

    def zip(self, where: str, data: bytes) -> None:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for info in z.infolist():
                if not info.is_dir():
                    self.file(f"{where}!{info.filename}", info.filename, z.read(info))

    def deb(self, where: str, data: bytes) -> None:
        # ar archive: "!<arch>\n", then 60-byte headers; data.tar.* holds the files.
        o = 8
        while o + 60 <= len(data):
            name = data[o:o + 16].decode().strip().rstrip("/")
            size = int(data[o + 48:o + 58].decode().strip())
            body = data[o + 60:o + 60 + size]
            if name.startswith("data.tar"):
                with tarfile.open(fileobj=io.BytesIO(body)) as t:
                    for m in t.getmembers():
                        if m.isfile():
                            self.file(f"{where}!{m.name}", m.name, t.extractfile(m).read())
            o += 60 + size + (size & 1)

    def path(self, p: Path) -> None:
        if p.is_dir():
            for d, _, names in os.walk(p):
                for n in names:
                    f = Path(d) / n
                    if f.is_file() and not f.is_symlink():
                        self.file(str(f), n, f.read_bytes())
                    elif f.is_symlink():
                        hit = match(n, self.spec["files"])
                        if hit:
                            self.findings.append(f"{f}: carries {n} (matches {hit})")
        else:
            self.file(str(p), p.name, p.read_bytes())

    def dpkg(self, root: Path) -> None:
        status = root / "var" / "lib" / "dpkg" / "status"
        if not status.is_file():
            self.unreadable.append(f"{status}: no dpkg status to read")
            return
        pkg = None
        for line in status.read_text(errors="replace").splitlines():
            if line.startswith("Package: "):
                pkg = line.split(": ", 1)[1].strip()
            elif line.startswith("Status: ") and "installed" in line.split()[-1:] and pkg:
                p = match(pkg, self.spec["packages"])
                if p:
                    self.findings.append(f"{root}: Debian package {pkg} is installed (matches {p})")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="*", type=Path)
    ap.add_argument("--spec", type=Path, default=SPEC)
    ap.add_argument("--dpkg-root", type=Path, action="append", default=[])
    a = ap.parse_args(argv)
    if not a.paths and not a.dpkg_root:
        ap.error("nothing to check")
    c = Checker(json.loads(a.spec.read_text()))
    for p in a.paths:
        if not p.exists():
            c.unreadable.append(f"{p}: does not exist")
            continue
        c.path(p)
    for r in a.dpkg_root:
        c.dpkg(r)
    for line in c.findings + c.unreadable:
        print(line)
    if c.findings:
        print(f"check-encumbered: FAILED, {len(c.findings)} finding(s)")
        return 1
    if c.unreadable:
        print("check-encumbered: FAILED, could not check everything")
        return 2
    print(f"check-encumbered: clean ({c.files} files, {c.binaries} native binaries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
