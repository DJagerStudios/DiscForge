"""GD-ROM dumps (.gdi / .chd) -> selfboot CD-R.   EXPERIMENTAL.

GD-ROM games expect their filesystem at LBA 45000 (start of the GD-ROM
high-density area) and usually hard-code that position.  The classic fix is to
binary-patch every game for LBA 11702; instead this places the CD-R's boot
session *at* LBA 45000 (session 1 becomes a 7.5 minute silent audio track), so
the game finds its data where it always looks, unmodified.  The price is
~79 MB of disc space: about 615 MB remain for the game.

What still can't be fixed automatically: games over that size, CD-DA music
tracks (not carried over), copy-protection checks and Windows CE titles.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

from . import ipbin, iso9660
from . import scramble as SCR
from . import sectors as S
from .disc import CD80_CAPACITY, Disc, DiscError, Track
from .homebrew import find_mkisofs, silent_session_for
from .util import cache_dir, fmt_mb, read_bytes
from . import tools

GD_LBA = 45000
MAX_DATA_SECTORS = CD80_CAPACITY - GD_LBA - 2


@dataclass
class GdiTrack:
    number: int
    lba: int
    ctrl: int
    sector_size: int
    path: str
    offset: int

    @property
    def is_data(self) -> bool:
        return self.ctrl == 4

    @property
    def sectors(self) -> int:
        return (os.path.getsize(self.path) - self.offset) // self.sector_size


def parse_gdi(path: str) -> list[GdiTrack]:
    with open(path, encoding="latin-1") as f:
        tokens = re.findall(r'"[^"]*"|\S+', f.read())
    try:
        count = int(tokens[0])
        base = os.path.dirname(os.path.abspath(path))
        tracks = []
        for i in range(count):
            num, lba, ctrl, size, name, off = tokens[1 + 6 * i:7 + 6 * i]
            tracks.append(GdiTrack(int(num), int(lba), int(ctrl), int(size),
                                   os.path.join(base, name.strip('"')), int(off)))
    except (ValueError, IndexError):
        raise DiscError("This .gdi file is malformed.") from None
    for t in tracks:
        if not os.path.exists(t.path):
            raise DiscError(f"Track file missing: {os.path.basename(t.path)}")
        if t.sector_size not in (2048, 2352):
            raise DiscError(f"Unsupported GDI sector size {t.sector_size}")
    return tracks


class _HDArea:
    """Absolute-LBA reader over the data tracks of the high-density area."""

    def __init__(self, tracks: list[GdiTrack]):
        self.parts = [(t.lba, S.FileSectorSource(t.path, t.offset, t.sectors, t.sector_size, S.MODE1))
                      for t in tracks if t.is_data and t.lba >= GD_LBA]
        if not self.parts or self.parts[0][0] != GD_LBA:
            raise DiscError("Not a GD-ROM dump: no data track at LBA 45000.")

    def read(self, lba: int, n: int = 1) -> bytes:
        for base, src in self.parts:
            if base <= lba < base + src.count:
                k = min(n, base + src.count - lba)
                data = src.read(lba - base, k)
                return data + (self.read(lba + k, n - k) if k < n else b"")
        raise IndexError(f"LBA {lba} is outside the GD-ROM data tracks")

    def copy(self, lba: int, size: int, out) -> None:
        left = size
        while left > 0:
            n = min(left, 512 * 2048)
            out.write(self.read(lba, (n + 2047) // 2048)[:n])
            lba += (n + 2047) // 2048
            left -= n


def _is_dummy(hd: _HDArea, e: iso9660.Entry) -> bool:
    if re.match(r"^(DUMMY|_DUMMY|PADDING|ZZZ)", e.name.upper()):
        return True
    if e.size < 8 << 20:
        return False
    head = hd.read(e.extent, 32)
    tail = hd.read(e.extent + e.size // 2048 - 32, 32)
    return not any(head) and not any(tail)


def load_gdi(path: str, title: str | None = None, progress=None) -> Disc:
    tracks = parse_gdi(path)
    hd = _HDArea(tracks)
    ip_raw = hd.read(GD_LBA, 16)
    info = ipbin.parse(ip_raw)
    if not info.valid:
        raise DiscError("No valid IP.BIN at LBA 45000; this isn't a Dreamcast GD-ROM.")
    fs = iso9660.IsoFS(lambda lba: hd.read(lba, 1), GD_LBA)
    entries = list(fs.walk())
    files = [e for e in entries if not e.is_dir]
    total = sum((e.size + 2047) // 2048 for e in files)
    dummies = [e for e in files if _is_dummy(hd, e)] if total > MAX_DATA_SECTORS - 500 else []
    skip = {id(e) for e in dummies}
    keep_total = total - sum((e.size + 2047) // 2048 for e in dummies)
    if keep_total > MAX_DATA_SECTORS - 500:
        raise DiscError(f"The game's data ({fmt_mb(keep_total)}) doesn't fit on a CD-R with this method "
                        f"(max about {fmt_mb(MAX_DATA_SECTORS)}). Look for a selfboot .cdi release instead.")

    key = hashlib.sha1(f"{os.path.abspath(path)}:{os.path.getmtime(path)}".encode()).hexdigest()[:10]
    work = os.path.join(cache_dir(), f"stage-gdi-{key}")
    shutil.rmtree(work, ignore_errors=True)
    root = os.path.join(work, "files")
    os.makedirs(root)
    try:
        order = []
        copied = 0
        for e in entries:
            rel = e.path.lstrip("/")
            if ".." in rel.split("/"):
                continue
            out = os.path.join(root, rel)
            if e.is_dir:
                os.makedirs(out, exist_ok=True)
                continue
            if id(e) in skip:
                continue
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as f:
                hd.copy(e.extent, e.size, f)
            order.append((e.extent, out))
            copied += (e.size + 2047) // 2048
            if progress:
                progress("Converting the GD-ROM…", 0.85 * copied / max(1, keep_total))

        boot_name = info.boot_filename.strip() or "1ST_READ.BIN"
        boot = fs.find(boot_name)
        notes: list[str] = []
        if boot is None:
            raise DiscError(f"Boot file {boot_name} not found on the GD-ROM.")
        boot_path = os.path.join(root, boot.path.lstrip("/"))
        data = read_bytes(boot_path)
        if SCR.looks_scrambled(data) is True:
            notes.append(f"{boot_name} is already scrambled; left as is.")
        else:
            with open(boot_path, "wb") as f:
                f.write(SCR.scramble(data))
            notes.append(f"{boot_name} scrambled for CD-R (MIL-CD) boot.")

        # keep the original on-disc file order (matters for streaming-heavy games)
        sort_path = os.path.join(work, "sort.txt")
        with open(sort_path, "w") as f:
            for rank, (_lba, p) in enumerate(sorted(order)):
                f.write(f"{p} {len(order) - rank}\n")
        ip_path = os.path.join(work, "IP.BIN")
        with open(ip_path, "wb") as f:
            f.write(ip_raw)
        iso = os.path.join(work, "data.iso")
        vol = (fs.volume_id or "DREAMCAST")[:32]
        cmd = [find_mkisofs(), "-quiet", "-C", f"0,{GD_LBA}", "-V", vol, "-G", ip_path, "-sort", sort_path,
               "-l", "-D", "-relaxed-filenames", "-allow-lowercase", "-allow-multidot", "-no-iso-translate",
               "-input-charset", "default", "-o", iso, root]          # 1:1, like ISO-8859-1; no tables
        if progress:
            progress("Building the CD image…", 0.9)
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL,
                           env=tools.env())
        if r.returncode != 0:
            raise DiscError("mkisofs failed: " + (r.stderr.strip() or r.stdout.strip())[-500:])
        shutil.rmtree(root, ignore_errors=True)   # the ISO is all we need now
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise

    src = S.FileSectorSource(iso, 0, os.path.getsize(iso) // 2048, 2048, S.MODE2)
    disc = Disc(os.path.basename(path), "gdi", [[silent_session_for(GD_LBA)],
                                                [Track(S.MODE2, GD_LBA, src, "data (rebuilt)")]], path)
    disc.workdir = work
    disc.warnings.append("Experimental GD-ROM conversion: the game is left unpatched and its data is placed at "
                         "LBA 45000, where GD-ROM games look for it. Many games work this way, but not all "
                         "(copy protection, Windows CE). Test in the emulator, and prefer a selfboot .cdi "
                         "release when one exists.")
    audio = [t for t in tracks if not t.is_data and t.lba >= GD_LBA]
    if audio:
        disc.warnings.append(f"The GD-ROM has {len(audio)} CD audio track(s); they aren't carried over, "
                             "so in-game CD music will be missing.")
    if dummies:
        disc.notes.append(f"Left out {len(dummies)} dummy padding file(s) "
                          f"({fmt_mb(sum((e.size + 2047) // 2048 for e in dummies))}) to make it fit.")
    disc.notes += notes
    disc.notes.append(f"Rebuilt {len(order)} files ({fmt_mb(keep_total)}) from the GD-ROM's high-density area.")
    return disc


def load_chd(path: str, title: str | None = None, progress=None) -> Disc:
    tool = tools.find("chdman")
    if not tool:
        raise DiscError("Opening .chd files needs chdman: sudo pacman -S mame-tools")
    key = hashlib.sha1(f"{os.path.abspath(path)}:{os.path.getmtime(path)}".encode()).hexdigest()[:12]
    dest = os.path.join(cache_dir(), f"extract-{key}")
    gdi = os.path.join(dest, "disc.gdi")
    if not os.path.exists(os.path.join(dest, ".done")):
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(dest)
        proc = subprocess.Popen([tool, "extractcd", "-i", path, "-o", gdi, "-f"], stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, env=tools.env())
        tail = b""
        assert proc.stderr
        try:
            while True:
                ch = proc.stderr.read1(4096)
                if not ch:
                    break
                tail = (tail + ch)[-400:]
                m = re.findall(rb"(\d+(?:\.\d+)?)% complete", tail)
                if m and progress:
                    progress("Unpacking the CHD…", float(m[-1]) / 100)
            rc = proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stderr.close()
        if rc != 0 or not os.path.exists(gdi):
            shutil.rmtree(dest, ignore_errors=True)
            raise DiscError("chdman couldn't extract this CHD as a GD-ROM: "
                            + tail.decode("utf-8", "replace").strip()[-300:])
        open(os.path.join(dest, ".done"), "w").close()
    disc = load_gdi(gdi, title, progress)
    disc.name, disc.path = os.path.basename(path), path
    return disc
