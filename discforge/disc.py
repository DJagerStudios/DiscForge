"""Disc model: every input (CDI, ISO, homebrew) becomes sessions of tracks."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable

from . import cdi as CDI
from . import ipbin, iso9660
from . import scramble as SCR
from . import sectors as S
from .util import DcError

BOOT_SESSION_LBA = 11702        # data session LBA after a 300-sector TAO audio session
CD80_CAPACITY = 359849          # sectors before the lead-out of a typical 80 min CD-R
MAX_BOOT_FILE = 0x8D000000 - 0x8C010000   # the boot ROM loads 1ST_READ.BIN at 0x8C010000 in 16 MB of RAM


class DiscError(DcError):
    pass


@dataclass
class Track:
    mode: str
    lba: int
    source: S.SectorSource
    label: str = ""

    @property
    def sectors(self) -> int:
        return self.source.count

    @property
    def is_data(self) -> bool:
        return self.mode != S.AUDIO


@dataclass
class Disc:
    name: str
    kind: str                                   # 'cdi', 'iso', 'homebrew', 'gdi'
    sessions: list[list[Track]]
    path: str | None = None
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)   # problems that make a burn pointless
    # homebrew discs can rebuild their filesystem for whatever LBA the drive gives us
    rebuild: Callable[[int], S.SectorSource] | None = None
    workdir: str | None = None                  # temporary files owned by this disc
    _fs: iso9660.IsoFS | None = None

    def cleanup(self) -> None:
        if self.workdir:
            import shutil
            shutil.rmtree(self.workdir, ignore_errors=True)
            self.workdir = None

    # -- structure -------------------------------------------------------
    @property
    def boot_track(self) -> Track:
        return self.sessions[-1][0]

    @property
    def layout(self) -> str:
        if len(self.sessions) == 1:
            return "single session"
        first = self.sessions[0]
        kind = "audio" if all(t.mode == S.AUDIO for t in first) else "data"
        name = f"{kind}/data"
        if len(first) > 1:
            name += f" ({len(first)} tracks in session 1)"
        if len(self.sessions) > 2:
            name += f", {len(self.sessions)} sessions"
        return name

    def track_at(self, lba: int) -> Track | None:
        for s in self.sessions:
            for t in s:
                if t.is_data and t.lba <= lba < t.lba + t.sectors:
                    return t
        return None

    def read_lba(self, lba: int) -> bytes:
        return self.read_lbas(lba, 1)

    def read_lbas(self, lba: int, n: int) -> bytes:
        """n user-data sectors from absolute LBA `lba` (may span data tracks)."""
        out = []
        while n > 0:
            t = self.track_at(lba)
            if t is None:
                raise IndexError(f"LBA {lba} is not inside a data track")
            k = min(n, t.lba + t.sectors - lba)
            out.append(t.source.read(lba - t.lba, k))
            lba += k
            n -= k
        return b"".join(out)

    @property
    def end_lba(self) -> int:
        t = self.sessions[-1][-1]
        return t.lba + t.sectors

    # -- boot information -----------------------------------------------
    def ip_raw(self) -> bytes | None:
        t = self.boot_track
        if not t.is_data or t.sectors < 16:
            return None
        return t.source.read(0, 16)

    def ip(self) -> ipbin.IpInfo | None:
        raw = self.ip_raw()
        if raw is None:
            return None
        info = ipbin.parse(raw)
        return info if info.valid else None

    def fs(self) -> iso9660.IsoFS | None:
        if self._fs is None:
            try:
                self._fs = iso9660.IsoFS(self.read_lba, self.boot_track.lba, read_range=self.read_lbas)
            except (iso9660.IsoError, IndexError, IOError):
                return None
        return self._fs

    def boot_file(self) -> tuple[iso9660.Entry | None, str]:
        info = self.ip()
        name = (info.boot_filename.strip() if info and info.boot_filename.strip() else "1ST_READ.BIN")
        fs = self.fs()
        return (fs.find(name) if fs else None), name

    def _scramble_in_place(self, entry: iso9660.Entry, data: bytes) -> bool:
        """Overlay a scrambled copy of the boot file on its own sectors (same size, so the
        filesystem is untouched)."""
        t = self.track_at(entry.extent)
        nsec = (entry.size + 2047) // 2048
        if t is None or entry.extent + nsec > t.lba + t.sectors:
            return False
        start = entry.extent - t.lba
        original = t.source.read(start, nsec)
        t.source = S.OverlaySource(t.source, start, SCR.scramble(data) + original[entry.size:])
        self._fs = None
        return True

    def analyse(self) -> None:
        """Check everything the Dreamcast's boot ROM will check, and note what's worth knowing."""
        bt = self.boot_track
        if not bt.is_data:
            self.blockers.append("The last session doesn't start with a data track, so this isn't a "
                                 "Dreamcast boot disc.")
            return
        info = self.ip()
        if info is None:
            self.blockers.append("No Dreamcast boot header (IP.BIN) at the start of the boot track. A Dreamcast "
                                 "would treat this disc as a plain CD.")
            return
        if info.wince:
            self.warnings.append("This is a Windows CE game. WinCE games often need extra patches to selfboot; "
                                 "test it in the emulator first.")
        if "GD-ROM" in info.device_info and self.kind != "homebrew":
            self.notes.append("IP.BIN still says GD-ROM: normal for selfboot conversions of commercial games.")
        fs = self.fs()
        if fs is None:
            self.blockers.append(f"No ISO 9660 filesystem where the boot track starts (LBA {bt.lba}), so the "
                                 "Dreamcast couldn't find the game's files.")
            return
        entry, name = self.boot_file()
        if entry is None:
            self.blockers.append(f"The game's start file {name} is missing from the disc's root folder.")
            return
        if entry.size == 0 or entry.size > MAX_BOOT_FILE:
            self.blockers.append(f"The start file {name} is {entry.size:,} bytes, which a Dreamcast can't load.")
            return
        data = fs.read_file(entry, limit=MAX_BOOT_FILE)
        head, whole = SCR.scramble_votes(data)
        state = SCR.looks_scrambled(data)
        if state is False and self.kind in ("cdi", "iso") and not info.wince and head is False and whole is False:
            if self._scramble_in_place(entry, data):
                self.notes.append(f"{name} wasn't prepared for CD-R, so a real Dreamcast would have crashed. "
                                  "It has been scrambled for you (the image file itself is unchanged).")
                return
        if state is False and self.kind in ("cdi", "iso"):
            self.warnings.append(f"{name} looks UNscrambled (GD-ROM / dc-load style). A real Dreamcast "
                                 "descrambles it when booting a CD-R, so it will crash.")
        elif state is True and self.kind not in ("homebrew", "gdi"):
            self.notes.append(f"{name} is scrambled correctly for CD-R (MIL-CD) boot.")


# --------------------------------------------------------------------------
# Loaders


def _check_boot_lba(disc: Disc) -> None:
    """Make the boot track's LBA match the LBA its filesystem was built for."""
    bt = disc.boot_track
    if not bt.is_data:
        return
    if iso9660.root_is_consistent(lambda lba: disc.read_lba(lba), bt.lba):
        return
    base = iso9660.infer_base(lambda i: bt.source.read(i, 1), bt.sectors)
    if base is None:
        return
    if base != bt.lba:
        disc.warnings.append(
            f"The image says the boot track is at LBA {bt.lba}, but its filesystem was built for LBA {base}; "
            f"using {base}.")
        bt.lba = base
        disc._fs = None


def load_cdi(path: str) -> Disc:
    img = CDI.read_cdi(path)
    sessions = []
    for s in img.sessions:
        tracks = []
        for t in s:
            src = t.source(path)
            tracks.append(Track(t.mode, t.lba, src, f"track {t.number}"))
        sessions.append(tracks)
    disc = Disc(os.path.basename(path), "cdi", sessions, path)
    disc.notes.append(f"DiscJuggler image, version {img.version_name}.")
    _check_boot_lba(disc)
    return disc


def iso_base(path: str) -> int:
    """The LBA a 2048-byte ISO image was built for (0 for an ordinary ISO)."""
    size = os.path.getsize(path)
    if size % 2048 or size < 17 * 2048:
        raise DiscError("not a 2048-byte-per-sector ISO image")
    count = size // 2048
    src = S.FileSectorSource(path, 0, count, 2048, S.MODE2)
    base = iso9660.infer_base(lambda i: src.read(i, 1), count)
    if base is None:
        raise DiscError("no ISO 9660 filesystem found in this file")
    return base


def load_iso(path: str) -> Disc | None:
    """A 2048-byte ISO.  Returns a Disc if it is a ready selfboot data track
    (IP.BIN in its system area, built for a non-zero LBA); otherwise None so
    the caller can treat its *contents* as homebrew."""
    base = iso_base(path)
    count = os.path.getsize(path) // 2048
    src = S.FileSectorSource(path, 0, count, 2048, S.MODE2)
    ip = ipbin.parse(src.read(0, 16))
    if base == 0 or not ip.valid:
        return None
    audio_len = base - 11402
    if audio_len < 300:
        raise DiscError(f"ISO was built for LBA {base}; that is too low for an audio/data selfboot disc")
    disc = Disc(os.path.basename(path), "iso", [
        [Track(S.AUDIO, 0, S.ZeroSource(audio_len + 2, S.RAW_SIZE), "audio (silence)")],
        [Track(S.MODE2, base, src, "data (ISO)")],
    ], path)
    disc.notes.append(f"Selfboot ISO built for LBA {base}; a {audio_len}-sector silent audio session will be added "
                      "in front of it.")
    return disc
