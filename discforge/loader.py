"""Open anything a user might drop on the burner and turn it into a Disc."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import struct
import subprocess
import zipfile

from . import cdi as CDI
from . import elf
from .disc import Disc, DiscError, load_cdi, load_iso
from .homebrew import homebrew_disc
from .util import cache_dir
from . import tools

ARCHIVE_EXTS = (".zip", ".7z", ".rar")
IMAGE_EXTS = (".cdi", ".gdi", ".iso", ".chd")


def _is_cdi(path: str) -> bool:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(size - 8)
            ver, off = struct.unpack("<II", f.read(8))
        return ver in CDI.VERSIONS and off != 0
    except (OSError, struct.error):
        return False


def _extract(path: str, progress=None) -> str:
    key = hashlib.sha1(f"{os.path.abspath(path)}:{os.path.getmtime(path)}".encode()).hexdigest()[:12]
    dest = os.path.join(cache_dir(), f"extract-{key}")
    if os.path.isdir(dest) and os.path.exists(os.path.join(dest, ".done")):
        os.utime(dest)
        return dest
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest)
    if path.lower().endswith(".zip"):
        try:
            z = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError) as e:
            shutil.rmtree(dest, ignore_errors=True)
            raise DiscError("This zip file is damaged or incomplete (maybe the download didn't finish).") from e
        with z:
            infos = z.infolist()
            total = sum(i.file_size for i in infos) or 1
            free = shutil.disk_usage(dest).free
            if free < total + (256 << 20):
                shutil.rmtree(dest, ignore_errors=True)
                raise DiscError(f"Not enough free disk space to unpack this archive: it needs {total / 1e9:.1f} GB "
                                f"and {free / 1e9:.1f} GB is free.")
            done = 0
            try:
                for i in infos:
                    z.extract(i, dest)    # zipfile strips absolute paths and '..'
                    done += i.file_size
                    if progress:
                        progress("Unpacking…", done / total)
            except (zipfile.BadZipFile, EOFError, NotImplementedError, RuntimeError, OSError) as e:
                shutil.rmtree(dest, ignore_errors=True)
                if isinstance(e, OSError) and e.errno == 28:                       # ENOSPC
                    raise DiscError("The disk filled up while unpacking this archive.") from e
                if isinstance(e, RuntimeError) and "password" in str(e).lower():
                    raise DiscError("This zip file is password-protected; unpack it yourself first.") from e
                raise DiscError(f"This zip file couldn't be unpacked: {e}") from e
    else:
        tool = tools.find("7z") or tools.find("7zz") or tools.find("7za")
        if not tool:
            raise DiscError("Extracting .7z/.rar needs 7-Zip: sudo pacman -S 7zip")
        # -snl-: never create symbolic links from the archive (they could point outside the folder)
        proc = subprocess.Popen([tool, "x", "-y", "-bsp1", "-bso0", "-snl-", f"-o{dest}", path],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=tools.env())
        buf = b""
        tail = b""
        assert proc.stdout
        try:
            while True:
                ch = proc.stdout.read1(4096)
                if not ch:
                    break
                buf = (buf + ch)[-200:]
                tail = (tail + ch)[-2000:]
                m = re.findall(rb"(\d{1,3})%", buf)
                if m and progress:
                    progress("Unpacking…", min(int(m[-1]), 100) / 100)
        finally:
            proc.stdout.close()
        if proc.wait() != 0:
            shutil.rmtree(dest, ignore_errors=True)
            msg = tail.decode("utf-8", "replace").strip()
            raise DiscError("Could not extract the archive: " + msg[-400:])
        for root, dirs, files in os.walk(dest):        # belt and braces: no links out of the folder
            for n in dirs + files:
                q = os.path.join(root, n)
                if os.path.islink(q):
                    os.unlink(q)
    open(os.path.join(dest, ".done"), "w").close()
    return dest


def _pick_image(folder: str, recursive: bool = True) -> str | None:
    """The single disc image inside an extracted archive (or at the top of a folder)."""
    found: dict[str, list[str]] = {e: [] for e in IMAGE_EXTS}
    for root, _dirs, files in os.walk(folder):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in found:
                found[ext].append(os.path.join(root, f))
        if not recursive:
            break
    for ext in (".cdi", ".gdi", ".chd", ".iso"):
        if len(found[ext]) == 1:
            return found[ext][0]
        if len(found[ext]) > 1:
            names = ", ".join(os.path.basename(p) for p in found[ext][:5])
            raise DiscError(f"The archive holds several images ({names}); extract it and open the one you want.")
    return None


def load_any(path: str, *, title: str | None = None, regions: str = "JUE", vga: bool = True,
             scrambled: bool | None = None, gd_lba: bool = False, progress=None) -> Disc:
    """Open a CDI/ISO/GDI image, an archive, a homebrew program or a homebrew folder.

    Homebrew options: `title`, `regions`, `vga` (generated IP.BIN), `scrambled`
    (None = detect) and `gd_lba` (put the boot session at LBA 45000 for programs
    that hard-code the GD-ROM data position)."""
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(path):
        raise DiscError(f"{path} does not exist")
    opts = dict(title=title, regions=regions, vga=vga, scrambled=scrambled, gd_lba=gd_lba, progress=progress)
    hb = dict(title=title, regions=regions, vga=vga, scrambled=scrambled, lba=45000 if gd_lba else 11702)

    if os.path.isdir(path):
        # a folder that is "a game folder with an image in it" vs. a homebrew file tree
        inner = None if _has_boot_program(path) else _pick_image(path, recursive=False)
        if inner:
            return load_any(inner, **opts)
        disc = homebrew_disc(path, **hb)
    else:
        ext = os.path.splitext(path)[1].lower()
        if ext in ARCHIVE_EXTS:
            folder = _extract(path, progress)
            inner = _pick_image(folder)
            if inner:
                return load_any(inner, **opts)
            entries = [e for e in os.listdir(folder) if e != ".done"]
            if len(entries) == 1 and os.path.isdir(os.path.join(folder, entries[0])):
                folder = os.path.join(folder, entries[0])
            disc = homebrew_disc(folder, **{**hb, "title": title or _title_from(path)})
            disc.name, disc.path = os.path.basename(path), path
        elif ext == ".cdi" or _is_cdi(path):
            disc = load_cdi(path)
        elif ext == ".gdi":
            from .gdi import load_gdi
            disc = load_gdi(path, title=title, progress=progress)
        elif ext == ".chd":
            from .gdi import load_chd
            disc = load_chd(path, title=title, progress=progress)
        elif ext == ".iso":
            disc = load_iso(path)
            if disc is None:
                disc = _iso_contents_as_homebrew(path, hb)
        elif ext in (".cue", ".mds", ".mdf", ".nrg", ".ccd", ".img"):
            raise DiscError(f"{ext} images aren't supported; Dreamcast selfboot images normally come as .cdi. "
                            "Convert it to CDI first, or open the homebrew files directly.")
        else:
            with open(path, "rb") as f:
                head = f.read(4)
            if not elf.is_elf(head) and ext not in (".bin", ".elf") and os.path.getsize(path) > 16 << 20:
                raise DiscError("Unrecognised file type.")
            disc = homebrew_disc(path, **hb)
    disc.analyse()
    return disc


def _title_from(path: str) -> str:
    from .ipbin import clean_title
    return clean_title(os.path.basename(path))


def _has_boot_program(folder: str) -> bool:
    try:
        names = {e.upper() for e in os.listdir(folder)}
    except OSError:
        return False
    return any(n == "1ST_READ.BIN" or n.endswith((".ELF", ".BIN")) for n in names)


def _iso_contents_as_homebrew(path: str, hb: dict) -> Disc:
    """An ISO that isn't a ready selfboot track: copy its files out and rebuild it as a selfboot disc.
    Works for ordinary ISOs (LBA 0) and for ISOs built for another LBA but missing IP.BIN."""
    from . import iso9660, sectors as S
    from .disc import iso_base
    count = os.path.getsize(path) // 2048
    base = iso_base(path)
    src = S.FileSectorSource(path, 0, count, 2048, S.MODE1)
    fs = iso9660.IsoFS(lambda lba: src.read(lba - base, 1), base, joliet=True,
                       read_range=lambda lba, n: src.read(lba - base, n))
    key = hashlib.sha1(os.path.abspath(path).encode()).hexdigest()[:12]
    dest = os.path.join(cache_dir(), f"extract-{key}")
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest)
    for e in fs.walk():
        rel = e.path.lstrip("/")
        if ".." in rel.split("/"):
            continue
        out = os.path.join(dest, rel)
        if e.is_dir:
            os.makedirs(out, exist_ok=True)
        else:
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as f:
                left, lba = e.size, e.extent
                while left > 0:
                    n = min(left, 1 << 20)
                    f.write(fs.read_extent(lba, n))
                    lba += (n + 2047) // 2048
                    left -= n
    disc = homebrew_disc(dest, **{**hb, "title": hb.get("title") or _title_from(path)})
    disc.name, disc.path = os.path.basename(path), path
    return disc
