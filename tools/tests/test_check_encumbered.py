"""tools/check-encumbered.py fails on what it must and passes our own
binaries (RADIO-SDR-PLAN §5.5: "the check's own test builds a stub library
at test time ... and asserts the check fails on it and passes on the real
wheel"). Every binary here is built at test time from a few lines of C, or
assembled byte by byte (Mach-O, which no Linux compiler here makes); none
is a codec, only something carrying a codec's names."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import struct
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parent.parent / "check-encumbered.py"
spec = importlib.util.spec_from_file_location("check_encumbered", TOOL)
ce = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ce)

STUB = "int NeAACDecDecode(void *h, void *i, unsigned char *b, unsigned long n) { return 0; }\n"
EXT = "int PyInit_dummy(void) { return 0; }\n"
USER = "extern int NeAACDecDecode(void*, void*, unsigned char*, unsigned long);\n" \
       "int PyInit_dummy(void) { return NeAACDecDecode(0, 0, 0, 0); }\n"
# Names only, as shack_sdr's loader has them: looked up, never linked.
STRINGS = 'const char *names[] = {"NeAACDecDecode", "aacDecoder_Open", "mbe_processAmbe3600x2450Framef"};\n' \
          "int PyInit_dummy(void) { return names[0][0]; }\n"


def check(*paths, roots=()) -> int:
    args = [str(p) for p in paths]
    for r in roots:
        args += ["--dpkg-root", str(r)]
    return ce.main(args)


def cc(tmp: Path, name: str, src: str, *args: str, compiler: str = "cc") -> Path:
    c = shutil.which(compiler)
    if not c:
        pytest.skip(f"no {compiler}")
    (tmp / f"{name}.c").write_text(src)
    out = tmp / name
    subprocess.run([c, "-shared", "-fPIC", "-O1", "-o", str(out), str(tmp / f"{name}.c"), *args], check=True,
                   cwd=tmp)
    return out


def test_a_codec_linked_into_an_extension_fails(tmp_path, capsys):
    ext = cc(tmp_path, "dummy.so", STUB + EXT)
    assert check(ext) == 1
    assert "defines NeAACDecDecode" in capsys.readouterr().out


def test_a_codec_linked_statically_but_hidden_still_fails(tmp_path):
    # Hidden from the dynamic table, still in the static symbol table that
    # an unstripped build keeps.
    src = STUB.replace("int NeAACDecDecode", '__attribute__((visibility("hidden"))) int NeAACDecDecode') + EXT
    assert check(cc(tmp_path, "hidden.so", src)) == 1


def test_importing_an_encumbered_library_fails(tmp_path, capsys):
    cc(tmp_path, "libfaad.so.2", STUB, "-Wl,-soname,libfaad.so.2")
    user = cc(tmp_path, "user.so", USER, str(tmp_path / "libfaad.so.2"))
    assert check(user) == 1
    assert "imports libfaad.so.2" in capsys.readouterr().out


def test_names_as_strings_are_fine(tmp_path):
    assert check(cc(tmp_path, "loader.so", STRINGS)) == 0


def test_windows_binaries_are_read_too(tmp_path, capsys):
    exporting = cc(tmp_path, "codec.dll", STUB, compiler="x86_64-w64-mingw32-gcc")
    assert check(exporting) == 1
    assert "pe defines NeAACDecDecode" in capsys.readouterr().out
    # Linked against a DLL of that name (the import is the DLL's own name).
    lib = cc(tmp_path, "libfaad-2.dll", STUB, compiler="x86_64-w64-mingw32-gcc")
    user = cc(tmp_path, "user.pyd", USER, str(lib), compiler="x86_64-w64-mingw32-gcc")
    lib.unlink()
    assert check(user) == 1
    assert "pe imports libfaad-2.dll" in capsys.readouterr().out
    assert check(cc(tmp_path, "clean.pyd", STRINGS, compiler="x86_64-w64-mingw32-gcc")) == 0


def macho(dylibs=(), defined=()) -> bytes:
    """A minimal 64-bit Mach-O: LC_LOAD_DYLIB per library, LC_SYMTAB with
    each name as a defined external symbol."""
    cmds = b""
    for lib in dylibs:
        name = lib.encode() + b"\0"
        name += b"\0" * (-(24 + len(name)) % 8)
        cmds += struct.pack("<IIIIII", 0xC, 24 + len(name), 24, 0, 0, 0) + name
    strtab = b"\0" + b"".join(b"_" + s.encode() + b"\0" for s in defined)
    header = 32
    symtab_cmd = 24
    symoff = header + len(cmds) + symtab_cmd
    nlist = b""
    at = 1
    for s in defined:
        nlist += struct.pack("<IBBHQ", at, 0x0F, 1, 0, 0x1000)   # N_SECT | N_EXT
        at += len(s) + 2
    stroff = symoff + len(nlist)
    cmds += struct.pack("<IIIIII", 0x2, symtab_cmd, symoff, len(defined), stroff, len(strtab))
    hdr = struct.pack("<IiiIIIII", 0xFEEDFACF, 0x0100000C, 0, 6, 1 + len(dylibs), len(cmds), 0, 0)
    return hdr + cmds + nlist + strtab


def test_macos_binaries_are_read_too(tmp_path, capsys):
    (tmp_path / "a.so").write_bytes(macho(["/usr/lib/libSystem.B.dylib"], ["PyInit__native"]))
    assert check(tmp_path / "a.so") == 0
    (tmp_path / "b.so").write_bytes(macho(["@loader_path/libfdk-aac.2.dylib"], ["PyInit__native"]))
    (tmp_path / "c.so").write_bytes(macho([], ["aacDecoder_DecodeFrame"]))
    # A universal binary: the bad half is found.
    thin = macho([], ["mbe_processImbe7200x4400Framef"])
    fat = struct.pack(">II", 0xCAFEBABE, 1) + struct.pack(">iiIII", 0x0100000C, 0, 4096, len(thin), 12)
    (tmp_path / "d.so").write_bytes(fat + b"\0" * (4096 - len(fat)) + thin)
    for name, what in (("b.so", "imports @loader_path/libfdk-aac.2.dylib"), ("c.so", "defines aacDecoder_"),
                       ("d.so", "defines mbe_processImbe")):
        assert check(tmp_path / name) == 1
        assert what in capsys.readouterr().out


def test_wheels_and_debs_are_opened(tmp_path, capsys):
    whl = tmp_path / "x-1-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as z:
        z.writestr("x/__init__.py", "")
        z.writestr("x/libfdk-aac.so.2", b"not even a library")
    assert check(whl) == 1
    assert "carries libfdk-aac.so.2" in capsys.readouterr().out
    # A .deb: ar, with data.tar.xz inside.
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:xz") as t:
        body = b"\x7fELF"
        info = tarfile.TarInfo("./usr/lib/x86_64-linux-gnu/libmbe.so.1")
        info.size = 0
        t.addfile(info, io.BytesIO(b""))
        ok = tarfile.TarInfo("./usr/lib/x86_64-linux-gnu/libmbedcrypto.so.16")   # not mbelib
        ok.size = 0
        t.addfile(ok, io.BytesIO(b""))
        del body

    def member(name, payload):
        h = f"{name:<16}{0:<12}{0:<6}{0:<6}{100644:<8}{len(payload):<10}`\n".encode()
        return h + payload + (b"\n" if len(payload) % 2 else b"")
    deb = b"!<arch>\n" + member("debian-binary", b"2.0\n") + member("data.tar.xz", data.getvalue())
    (tmp_path / "x.deb").write_bytes(deb)
    assert check(tmp_path / "x.deb") == 1
    out = capsys.readouterr().out
    assert "carries libmbe.so.1" in out and "libmbedcrypto" not in out


def test_a_store_image_with_a_denylisted_package_fails(tmp_path, capsys):
    status = tmp_path / "var" / "lib" / "dpkg"
    status.mkdir(parents=True)
    (status / "status").write_text("Package: libcodec2-1.2\nStatus: install ok installed\n\n"
                                   "Package: libfaad2\nStatus: install ok installed\n\n")
    assert check(roots=[tmp_path]) == 1
    assert "libfaad2 is installed" in capsys.readouterr().out
    (status / "status").write_text("Package: libcodec2-1.2\nStatus: install ok installed\n\n"
                                   "Package: libfaad2\nStatus: deinstall ok config-files\n\n")
    assert check(roots=[tmp_path]) == 0


def test_an_unreadable_binary_is_not_clean(tmp_path):
    (tmp_path / "broken.so").write_bytes(b"\x7fELF\x02\x01\x01" + b"\xff" * 20)
    assert check(tmp_path / "broken.so") == 2


def test_shack_sdrs_own_extension_is_clean():
    """The real thing, where tools/test.sh has built it (SHACK_SDR_BUILT)."""
    built = os.environ.get("SHACK_SDR_BUILT")
    if not built or not Path(built).exists():
        pytest.skip("shack_sdr's extension is not built in this run")
    assert check(Path(built)) == 0


def test_the_spec_is_well_formed():
    s = json.loads(ce.SPEC.read_text())
    assert set(s) >= {"libraries", "files", "symbols", "packages"}
    assert ce.match("libmbedcrypto.so.16", s["files"]) is None
    assert ce.match("libmbe.so.1", s["files"])
