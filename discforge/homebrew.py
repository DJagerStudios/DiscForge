"""Build a selfboot disc from homebrew: an ELF, a raw program binary, or a folder."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

from . import elf, ipbin
from . import scramble as SCR
from . import sectors as S
from .disc import BOOT_SESSION_LBA, MAX_BOOT_FILE, Disc, DiscError, Track
from .util import cache_dir, read_bytes
from . import tools

PROGRAM_EXTS = (".elf", ".bin")


@dataclass
class Stage:
    dir: str
    boot_name: str
    ip: bytes
    title: str
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def find_mkisofs() -> str:
    for name in ("mkisofs", "genisoimage"):
        p = tools.find(name)
        if p:
            return p
    raise DiscError("mkisofs was not found. Install it with: sudo pacman -S cdrtools")


JUNK = {".git", ".svn", ".hg", "__pycache__", ".ds_store", "thumbs.db", "desktop.ini", ".directory"}


def _junk(_folder: str, names: list[str]) -> set[str]:
    """Files that only belong to the PC (version control, file-manager metadata)."""
    return {n for n in names if n.lower() in JUNK or n.startswith("._")}


def _boot_binary(data: bytes, label: str, scrambled: bool | None, st: Stage) -> bytes:
    """Return the program as a *scrambled* 1ST_READ.BIN."""
    if elf.is_elf(data):
        binary, base, entry = elf.elf_to_bin(data)
        if base & 0x1FFFFFFF != elf.LOAD_ADDR & 0x1FFFFFFF:
            raise DiscError(f"{label} is linked at 0x{base:08X}; a Dreamcast CD always loads its program at "
                            "0x8C010000, so it can't start from a CD.")
        st.warnings += elf.check_load_address(base, entry)
        if len(binary) > MAX_BOOT_FILE:
            raise DiscError(f"{label} is too big to fit in the Dreamcast's memory.")
        st.notes.append(f"{label}: converted ELF to a {len(binary):,}-byte binary and scrambled it.")
        return SCR.scramble(binary)
    if len(data) > MAX_BOOT_FILE:
        raise DiscError(f"{label} is {len(data) / 2**20:.0f} MB: too big to be a Dreamcast program "
                        "(if it is a disc image, open its .cue/.gdi/.cdi instead).")
    detected = SCR.looks_scrambled(data)
    if scrambled is None:
        scrambled = detected
        if scrambled is None:
            scrambled = False
            st.warnings.append(f"Couldn't tell whether {label} is already scrambled; assuming it is NOT "
                               "(dc-load/GDEMU style) and scrambling it. Test in the emulator before burning.")
    elif detected is not None and detected != scrambled:
        st.warnings.append(f"You said {label} is {'already' if scrambled else 'not'} scrambled, "
                           f"but it looks {'scrambled' if detected else 'unscrambled'}.")
    if scrambled:
        st.notes.append(f"{label} is already scrambled for CD-R boot; used as is.")
        return data
    st.notes.append(f"{label} is an unscrambled program; scrambled it for CD-R (MIL-CD) boot.")
    return SCR.scramble(data)


def stage(path: str, *, title: str | None = None, regions: str = "JUE", vga: bool = True,
          scrambled: bool | None = None) -> Stage:
    """Copy the program and its data into a staging folder, ready for mkisofs."""
    root = tempfile.mkdtemp(prefix="stage-", dir=cache_dir())
    files = os.path.join(root, "files")
    os.makedirs(files)
    name = os.path.basename(os.path.normpath(path))
    st = Stage(files, "1ST_READ.BIN", b"", (title or ipbin.clean_title(name)))
    ip_raw = None
    try:
        if os.path.isdir(path):
            shutil.copytree(path, files, dirs_exist_ok=True, symlinks=False, ignore=_junk)
            entries = {e.upper(): e for e in os.listdir(files)}
            if "IP.BIN" in entries:
                cand = read_bytes(os.path.join(files, entries["IP.BIN"]))
                if len(cand) >= ipbin.IP_SIZE and ipbin.parse(cand).valid:
                    ip_raw = cand[:ipbin.IP_SIZE]
                    st.notes.append("Using the folder's own IP.BIN as the bootstrap.")
                os.remove(os.path.join(files, entries.pop("IP.BIN")))
            if ip_raw:
                st.boot_name = ipbin.parse(ip_raw).boot_filename.strip() or "1ST_READ.BIN"
            boot = entries.get(st.boot_name.upper())
            if boot is None:
                progs = [e for u, e in entries.items() if u.lower().endswith(PROGRAM_EXTS)
                         and os.path.isfile(os.path.join(files, e))]
                if len(progs) != 1:
                    raise DiscError(f"No {st.boot_name} in the folder, and no single .elf/.bin program to use instead.")
                boot = progs[0]
                st.notes.append(f"No {st.boot_name}; using {boot} as the program.")
            data = read_bytes(os.path.join(files, boot))
            os.remove(os.path.join(files, boot))
            out = _boot_binary(data, boot, scrambled, st)
        else:
            data = read_bytes(path)
            out = _boot_binary(data, name, scrambled, st)
        with open(os.path.join(files, st.boot_name.upper()), "wb") as f:
            f.write(out)
        if ip_raw is None:
            ip_raw = ipbin.build(st.title[:128], regions=regions, vga=vga, boot_filename=st.boot_name.upper())
            st.notes.append(f"Generated IP.BIN: title \"{st.title}\", regions {regions or 'none'}, "
                            f"VGA {'on' if vga else 'off'}.")
        st.ip = ip_raw
        return st
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


def build_iso(st: Stage, lba: int, out_path: str) -> str:
    ip_path = out_path + ".ip"
    with open(ip_path, "wb") as f:
        f.write(st.ip)
    vol = "".join(c if c.isalnum() else "_" for c in st.title.upper())[:32] or "DREAMCAST"
    cmd = [find_mkisofs(), "-quiet", "-C", f"0,{lba}", "-V", vol, "-G", ip_path,
           "-l", "-J", "-joliet-long", "-r", "-D", "-input-charset", "utf-8", "-o", out_path, st.dir]
    r = subprocess.run(cmd, capture_output=True, text=True)
    os.remove(ip_path)
    if r.returncode != 0:
        raise DiscError("mkisofs failed: " + (r.stderr.strip() or r.stdout.strip()))
    return out_path


def silent_session_for(lba: int) -> Track:
    """Session 1 sized so that the next session starts at `lba` (image convention: +2 run-out)."""
    audio = lba - 11402
    if audio < 300:
        raise DiscError(f"boot LBA {lba} is too low (minimum 11702)")
    return Track(S.AUDIO, 0, S.ZeroSource(audio + 2, S.RAW_SIZE), "audio (silence)")


def homebrew_disc(path: str, *, lba: int = BOOT_SESSION_LBA, **kw) -> Disc:
    """`lba=45000` puts the boot session where GD-ROM software expects its data
    (for programs that hard-code the GD-ROM position, e.g. GDEMU menus)."""
    st = stage(path, **kw)
    work = os.path.dirname(st.dir)

    def make_source(at: int) -> S.SectorSource:
        iso = build_iso(st, at, os.path.join(work, f"data-{at}.iso"))
        return S.FileSectorSource(iso, 0, os.path.getsize(iso) // 2048, 2048, S.MODE2)

    data = make_source(lba)
    disc = Disc(os.path.basename(os.path.normpath(path)), "homebrew", [
        [silent_session_for(lba)],
        [Track(S.MODE2, lba, data, "data")],
    ], path)
    disc.notes += st.notes
    disc.warnings += st.warnings
    if lba != BOOT_SESSION_LBA:
        disc.notes.append(f"Boot session placed at LBA {lba} (GD-ROM data position).")
    else:
        disc.rebuild = make_source   # LBA-independent: can follow whatever the drive gives us
    disc.workdir = work
    return disc
