"""Find games already on this computer (for the library shelf), with their console."""
from __future__ import annotations

import os
import re
import subprocess
import time
import zipfile
from dataclasses import dataclass
from typing import Callable

from . import ipbin
from . import tools

GAME_EXTS = {".cdi": "CDI", ".gdi": "GDI", ".chd": "CHD", ".cue": "CUE", ".ccd": "CCD", ".iso": "ISO",
             ".gcm": "GCM", ".xiso": "XISO", ".wbfs": "WBFS", ".rvz": "RVZ", ".wia": "WIA", ".gcz": "GCZ",
             ".cso": "CSO", ".zso": "ZSO", ".ecm": "ECM"}
ARCHIVE_EXTS = {".zip": "ZIP", ".7z": "7Z", ".rar": "RAR"}
INNER = tuple(GAME_EXTS)
SKIP_DIRS = {"node_modules", "__pycache__", "site-packages", "steamapps", "compatdata", "proton", "Proton",
             "shadercache", "Steam", "lost+found", "venv", ".git"}
REGION_WORDS = [("Japan", "J"), ("USA", "U"), ("US", "U"), ("NTSC-U", "U"), ("Europe", "E"), ("PAL", "E"),
                ("EU", "E"), ("World", "JUE")]


@dataclass
class Found:
    path: str
    kind: str          # CDI / GDI / CHD / CUE / ISO / WBFS / ... / ZIP / 7Z / RAR
    title: str
    regions: str       # subset of "JUE", "" when unknown
    size: int
    mtime: float
    platform: str = "unknown"
    game_id: str = ""


def default_roots() -> list[str]:
    home = os.path.expanduser("~")
    names = ["Downloads", "Desktop", "Documents", "Games", "games", "ROMs", "Roms", "roms", "Emulation",
             "Dreamcast", "dreamcast", "RetroArch", "Retro", "Emulators"]
    roots = [os.path.join(home, n) for n in names]
    user = os.environ.get("USER", "")
    for media in (f"/run/media/{user}", f"/media/{user}"):
        try:
            roots += [os.path.join(media, d) for d in os.listdir(media)]
        except OSError:
            pass
    seen, out = set(), []
    for r in roots:
        try:
            real = os.path.realpath(r)
        except OSError:
            continue
        if os.path.isdir(real) and real not in seen:
            seen.add(real)
            out.append(r)
    return out


REGION_TOKENS = {"pal": "E", "eur": "E", "europe": "E", "eu": "E", "ntsc": "U", "usa": "U", "us": "U",
                 "ntscu": "U", "jap": "J", "jpn": "J", "japan": "J", "ntscj": "J", "jp": "J"}


def title_from_name(path: str) -> tuple[str, str]:
    """A readable title and region guess from a file name like 'Sonic Adventure (USA) (Rev 1).cdi'."""
    stem = os.path.splitext(os.path.basename(path))[0]
    if re.fullmatch(r"(disc|track\d*|game|image|cd\d*)", stem, re.I):
        stem = os.path.basename(os.path.dirname(path)) or stem
    regions = ""
    for word, code in REGION_WORDS:
        if re.search(rf"[\(\[]([^)\]]*,\s*)?{re.escape(word)}(\s*,[^)\]]*)?[\)\]]", stem, re.I):
            regions += code
    title = re.sub(r"[\(\[][^)\]]*[\)\]]", " ", stem)
    title = re.sub(r"[_\.]+", " ", title)
    words = []
    for w in title.split():
        code = REGION_TOKENS.get(w.lower().replace("-", ""))
        head, _, tail = w.rpartition("-")
        if code:
            regions += code
        elif head and REGION_TOKENS.get(tail.lower()):      # "ikaruga-jap"
            regions += REGION_TOKENS[tail.lower()]
            words.append(head)
        else:
            words.append(w)
    title = " ".join(words).strip(" -")
    regions = "".join(c for c in "JUE" if c in regions)
    return smart_case(title or stem), regions


ACRONYMS = {"NBA", "NFL", "NHL", "NCAA", "WWF", "WWE", "UFC", "ESPN", "GT", "RPG", "DX", "EX", "HD", "VR",
            "DC", "SNK", "KOF", "TNN", "ECW", "MTV", "BMX", "ATV", "4X4", "USA", "UK", "PAL", "NTSC"}
SMALL_WORDS = {"of", "the", "and", "a", "an", "in", "on", "to", "vs", "vs.", "for", "at", "by", "or"}
ROMAN = re.compile(r"(?=[MDCLXVI])M*(C[MD]|D?C{0,3})(X[CL]|L?X{0,3})(I[XV]|V?I{0,3})")


def smart_case(t: str) -> str:
    """'THE HOUSE OF THE DEAD 2' -> 'The House of the Dead 2'; leaves mixed-case titles alone."""
    t = re.sub(r"\s+", " ", t).strip()
    letters = [c for c in t if c.isalpha()]
    if not letters or not (all(c.isupper() for c in letters) or all(c.islower() for c in letters)):
        return t
    words = []
    for i, w in enumerate(t.split(" ")):
        up = w.upper()
        if up in ACRONYMS or any(ch.isdigit() for ch in w) or (len(up) > 1 and ROMAN.fullmatch(up)):
            words.append(up)
        elif i and w.lower() in SMALL_WORDS:
            words.append(w.lower())
        else:
            words.append(w[:1].upper() + w[1:].lower())
    return " ".join(words)


def _nice_ip_title(t: str) -> str:
    return smart_case(t)


def quick_info(path: str) -> tuple[str | None, str]:
    """(title, regions) read from the disc's IP.BIN without building anything; (None, '') if unknown."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".cdi":
            from .cdi import read_cdi
            img = read_cdi(path)
            t = img.sessions[-1][0]
            if t.mode == "audio":
                return None, ""
            raw = t.source(path).read(0, 16)
        elif ext == ".gdi":
            from .gdi import parse_gdi
            from . import sectors as S
            tr = [x for x in parse_gdi(path) if x.is_data and x.lba == 45000]
            if not tr:
                return None, ""
            t = tr[0]
            raw = S.FileSectorSource(t.path, t.offset, 16, t.sector_size, S.MODE1).read(0, 16)
        else:
            return None, ""
    except Exception:
        return None, ""
    info = ipbin.parse(raw)
    if not info.valid or not info.title.strip():
        return None, ""
    return _nice_ip_title(info.title), info.regions


def describe(path: str, kind: str) -> tuple[str | None, str, str | None, str]:
    """(title, regions, platform, game id) for the shelf; platform None = not a game, skip it."""
    from . import detect
    ext = os.path.splitext(path)[1].lower()
    if ext in (".cdi", ".gdi"):
        title, regions = quick_info(path)
        return title, regions, "dreamcast", ""
    if ext in ARCHIVE_EXTS:
        return None, "", _archive_platform.get(path, detect.guess_platform(path)), ""
    if ext == ".ecm":
        return None, "", detect.guess_platform(path), ""
    try:
        ident = detect.identify(path)
    except Exception:
        return None, "", detect.guess_platform(path), ""
    if ident.platform == "unknown" and ident.blockers and ext == ".iso":
        return None, "", None, ""          # not a disc image we know
    if ident.platform == "pc" and ext == ".iso" and ident.media != "cd":
        return None, "", None, ""          # big PC ISOs (Linux installers ...) aren't shelf material
    plat = ident.platform if ident.platform not in ("unknown", "cd") else detect.guess_platform(path)
    if plat == "unknown" and ident.platform == "cd":
        plat = "cd"
    if plat == "dreamcast" and ext == ".chd":
        return None, "", plat, ""
    return None if ident.title == title_from_name(path)[0] else ident.title, "", plat, ident.game_id


_archive_platform: dict[str, str] = {}
INNER_PLATFORM = {".cdi": "dreamcast", ".gdi": "dreamcast", ".wbfs": "wii", ".rvz": "gamecube", ".gcz": "gamecube",
                  ".gcm": "gamecube", ".xiso": "xbox", ".cso": "ps2", ".zso": "ps2"}


def _archive_has_game(path: str, tool: str | None, deadline: float) -> bool:
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".zip":
            with zipfile.ZipFile(path) as z:
                return _note_inner(path, z.namelist())
        if tool and time.monotonic() < deadline:
            r = subprocess.run([tool, "l", "-ba", "-slt", path], capture_output=True, text=True,
                               errors="replace", timeout=max(1.0, min(5.0, deadline - time.monotonic())))
            names = [ln[7:] for ln in r.stdout.splitlines() if ln.startswith("Path = ")]
            return _note_inner(path, names)
    except (OSError, zipfile.BadZipFile, subprocess.TimeoutExpired, RuntimeError):
        pass
    return False


def _note_inner(path: str, names: list[str]) -> bool:
    from . import detect
    games = [n for n in names if n.lower().endswith(INNER) or n.upper().endswith("1ST_READ.BIN")]
    if not games:
        return False
    for n in games:
        plat = INNER_PLATFORM.get(os.path.splitext(n)[1].lower())
        if n.upper().endswith("1ST_READ.BIN"):
            plat = "dreamcast"
        plat = plat or detect.guess_platform(n)
        if plat != "unknown":
            _archive_platform[path] = plat
            break
    return True


def scan(roots: list[str] | None = None, *, max_depth: int = 4, archive_budget: float = 10.0,
         on_found: Callable[[Found], None] | None = None,
         should_stop: Callable[[], bool] = lambda: False, max_entries: int = 120_000) -> list[Found]:
    roots = default_roots() if roots is None else roots
    tool = tools.find("7z") or tools.find("7zz") or tools.find("7za")
    found: list[Found] = []
    seen: set[str] = set()
    archives: list[tuple[str, os.stat_result]] = []
    budget = [max_entries]

    def add(path: str, kind: str, st: os.stat_result) -> None:
        real = os.path.realpath(path)
        if real in seen:
            return
        seen.add(real)
        title, regions, platform, game_id = describe(path, kind)
        if platform is None:
            return
        name_title, name_regions = title_from_name(path)
        f = Found(path, kind, title or name_title, regions or name_regions, st.st_size, st.st_mtime, platform,
                  game_id)
        found.append(f)
        if on_found:
            on_found(f)

    def walk(d: str, depth: int) -> None:
        if depth > max_depth or should_stop() or budget[0] <= 0:
            return
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        budget[0] -= len(entries)
        for e in entries:
            if should_stop():
                return
            name = e.name
            if name.startswith("."):
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    if name not in SKIP_DIRS:
                        walk(e.path, depth + 1)
                    continue
                if not e.is_file():
                    continue
                ext = os.path.splitext(name)[1].lower()
                if ext in GAME_EXTS:
                    st = e.stat()
                    if st.st_size >= 512 * 1024 or ext in (".cue", ".ccd"):
                        add(e.path, GAME_EXTS[ext], st)
                elif ext in ARCHIVE_EXTS:
                    st = e.stat()
                    if 256 * 1024 <= st.st_size <= 3 << 30:
                        archives.append((e.path, st))
            except OSError:
                continue

    for r in roots:
        walk(r, 0)
    # archives last: listing them costs more, and only a few can hold games
    deadline = time.monotonic() + archive_budget
    archives.sort(key=lambda a: -a[1].st_mtime)
    for path, st in archives:
        if should_stop() or time.monotonic() > deadline:
            break
        if _archive_has_game(path, tool, deadline):
            add(path, ARCHIVE_EXTS[os.path.splitext(path)[1].lower()], st)
    found.sort(key=lambda f: -f.mtime)
    return found
