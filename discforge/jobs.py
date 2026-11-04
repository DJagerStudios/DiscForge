"""From "a file the user picked" to "a disc being burned", for every supported system.

    job = open_job(path, progress=..., cancel=...)   # identify, unpack, convert, plan
    result = burn_job(job, BurnOptions(dev), log, events)
    job.cleanup()

Conversions (archives, WBFS, CSO/ZSO, GCZ, RVZ/WIA, CHD, ECM, Xbox Redump -> XISO, folders)
happen in open_job, so a burn only ever starts from a finished, checked image: a failed
conversion never costs a disc.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable

from . import cue as CUE
from . import detect as D
from . import formats as F
from . import platforms as P
from . import xbox as X
from .burner import Burner, BurnError, BurnOptions, BurnResult
from .cdraw import CdPlan, CdrdaoBurner, VerifyRange
from .dvd import DvdPlan, dvd_burner
from .util import DcError, DirLock, cache_dir, in_use, remove_dir
from . import tools

Progress = Callable[[str, float], None]   # (what is happening, 0..1 or -1 for "busy")


class JobError(DcError):
    pass


@dataclass
class Job:
    ident: D.Identity
    method: str                       # dc | cd | dvd
    title: str
    source: str
    media: str                        # cd | dvd | dvd-dl
    sectors: int                      # needed on the disc (CD frames or DVD sectors)
    cd: CdPlan | None = None
    dvd: DvdPlan | None = None
    disc: object | None = None        # Dreamcast: loader Disc
    plan: object | None = None        # Dreamcast: BurnPlan
    work: str | None = None
    extra: list[str] = field(default_factory=list)      # more folders owned by the job
    locks: list[DirLock] = field(default_factory=list)  # cache folders the job reads from
    game_id: str = ""
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    details: list[tuple[str, str]] = field(default_factory=list)

    @property
    def platform(self) -> P.Platform:
        return P.get(self.ident.platform)

    @property
    def size_bytes(self) -> int:
        return self.sectors * (2352 if self.method == "cd" else 2048)

    @property
    def ok(self) -> bool:
        return not self.blockers

    def cleanup(self) -> None:
        for lock in self.locks:
            lock.release()
        self.locks = []
        if self.disc is not None and hasattr(self.disc, "cleanup"):
            self.disc.cleanup()
        for d in [self.work] + self.extra:
            if d:
                shutil.rmtree(d, ignore_errors=True)
        self.work, self.extra = None, []


# --------------------------------------------------------------------------
# Work folders

def _key(path: str) -> str:
    st = os.stat(path)
    return hashlib.sha1(f"{os.path.abspath(path)}:{st.st_mtime}:{st.st_size}".encode()).hexdigest()[:12]


def work_dir(tag: str) -> str:
    d = os.path.join(cache_dir(), f"job-{tag}-{os.getpid()}-{os.urandom(3).hex()}")
    os.makedirs(d)
    return d


def conversion_dir(path: str) -> tuple[str, bool, DirLock]:
    """Cache folder for a converted image, reused while the source is unchanged (and locked while a
    job uses it).  Only the most recent conversion is kept, since they can be many GB; folders another
    DiscForge window is using are left alone."""
    key = _key(path)
    d = os.path.join(cache_dir(), f"conv-{key}")
    for name in os.listdir(cache_dir()):
        if name.startswith("conv-") and name != f"conv-{key}":
            remove_dir(os.path.join(cache_dir(), name))
    done = os.path.exists(os.path.join(d, ".done"))
    if done:
        os.utime(d)
        return d, True, DirLock(d)
    if os.path.isdir(d):
        if in_use(d):
            raise JobError("This game is being prepared in another DiscForge window. Try again when it's done.")
        shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    return d, False, DirLock(d)


def _done(d: str) -> None:
    with open(os.path.join(d, ".done"), "w"):
        pass


def clear_conversions() -> None:
    for name in os.listdir(cache_dir()):
        if name.startswith(("conv-", "job-")):
            remove_dir(os.path.join(cache_dir(), name))


def ensure_space(folder: str, need: int) -> None:
    free = shutil.disk_usage(folder).free
    if free < need + (256 << 20):
        raise JobError(f"Not enough free disk space to prepare this game: it needs {need / 1e9:.1f} GB in "
                       f"{folder}, but only {free / 1e9:.1f} GB is free.")


# --------------------------------------------------------------------------

def _is_cd_image(p: str) -> bool:
    try:
        return D.synth_cue(p) is not None
    except CUE.CueError:                         # a raw image with audio: still a CD image, not a program
        return True


def pick_game(folder: str) -> str | None:
    """The one game inside an unpacked archive/folder (cue preferred over its bins)."""
    found: list[str] = []
    for root, _dirs, files in os.walk(folder):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in D.GAME_EXTS and not f.startswith("."):
                found.append(os.path.join(root, f))
    if not found:
        return None
    order = [".cue", ".gdi", ".cdi", ".ccd", ".chd", ".rvz", ".wia", ".gcz", ".wbfs", ".iso", ".gcm", ".xiso",
             ".cso", ".zso", ".img", ".ecm", ".toc", ".bin", ".dvd"]
    for ext in order:
        hits = [p for p in found if p.lower().endswith(ext)]
        if ext == ".ecm":                        # Game.bin.ecm next to its Game.cue is handled via the cue
            hits = [p for p in hits if not D.find_cue_for(p[:-4])]
        if ext == ".bin":
            hits = [p for p in hits if not D.find_cue_for(p)]
            if hits and not any(_is_cd_image(p) for p in hits):
                return None          # program files (homebrew), not CD images
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            names = ", ".join(os.path.basename(p) for p in hits[:4])
            raise JobError(f"This holds several games or discs ({names}). Unpack it and pick the one you want.")
    return None


def _cue_files(cue: str) -> list[str]:
    with open(cue, encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    names = []
    for line in text.splitlines():
        m = re.match(r'\s*FILE\s+"([^"]+)"', line, re.I) or re.match(r"\s*FILE\s+(\S+)", line, re.I)
        if m:
            names.append(m.group(1))
    return names


def _find_next_to(folder: str, name: str) -> str | None:
    base = os.path.basename(name.replace("\\", "/")).lower()
    try:
        for e in os.listdir(folder):
            if e.lower() == base:
                return os.path.join(folder, e)
    except OSError:
        pass
    return None


def _ecm_fixup(cue: str, rep, cancel) -> tuple[str, DirLock] | None:
    """A CUE whose bins exist only as .ecm files: decode them and write a CUE that points at them."""
    folder = os.path.dirname(os.path.abspath(cue))
    names = _cue_files(cue)
    packed = {n: _find_next_to(folder, n + ".ecm") for n in names if not _find_next_to(folder, n)}
    if not packed or not all(packed.values()):
        return None
    out_dir, done, lock = conversion_dir(cue)
    try:
        if not done:
            ensure_space(out_dir, sum(os.path.getsize(p) for p in packed.values()) * 3)
            for k, (name, ecm) in enumerate(packed.items()):
                dst = os.path.join(out_dir, os.path.basename(name.replace("\\", "/")))
                F.ecm_decode(ecm, dst + ".part", lambda f, k=k: rep("Unpacking the game…", (k + f) / len(packed)),
                             cancel)
                os.replace(dst + ".part", dst)
            _rewrite_cue(cue, out_dir)
            _done(out_dir)
    except BaseException:
        lock.release()
        raise
    return os.path.join(out_dir, os.path.basename(cue)), lock


def open_job(path: str, *, progress: Progress | None = None, cancel=None, opts: dict | None = None) -> Job:
    """Identify, unpack/convert and plan.  Raises JobError/DcError for hard failures; soft problems
    land in job.blockers / job.warnings."""
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(path):
        raise JobError(f"{path} doesn't exist.")
    if os.path.isfile(path) and os.path.getsize(path) == 0:
        raise JobError(f"{os.path.basename(path)} is empty (0 bytes), so there's nothing to burn. "
                       "The download may not have finished.")
    rep = progress or (lambda t, f: None)
    if path.lower().endswith(".cue"):
        fixed = _ecm_fixup(path, rep, cancel)
        if fixed:
            new_cue, lock = fixed
            try:
                job = open_job(new_cue, progress=progress, cancel=cancel, opts=opts)
            except BaseException:
                lock.release()
                raise
            job.locks.append(lock)
            job.title = D.title_from_name(path) if job.title.lower() in ("", "game") else job.title
            job.source = path
            job.details.insert(0, ("Original", "ECM"))
            return job
    ident = D.identify(path)
    if ident.container == "archive":
        from .loader import _extract
        folder = _extract(path, lambda t, f: rep("Unpacking…", f))
        lock = DirLock(folder)
        try:
            inner = pick_game(folder)
            target = inner or folder
            if not inner:
                entries = [e for e in os.listdir(folder) if e not in (".done", ".lock")]
                if len(entries) == 1 and os.path.isdir(os.path.join(folder, entries[0])):
                    target = os.path.join(folder, entries[0])
            job = open_job(target, progress=progress, cancel=cancel, opts=opts)
        except BaseException:
            lock.release()
            raise
        job.locks.append(lock)
        if job.title.lower() in ("", "game", "disc", "image") or not inner:
            job.title = D.title_from_name(path)
        job.source = path
        return job
    if ident.method == "folder":
        return _folder_job(path, ident, rep, cancel, opts)
    if ident.method == "dc":
        return _dc_job(path, ident, rep, opts)
    if ident.blockers and not ident.container:
        return _blocked(ident)
    c = ident.container
    if c in ("wbfs", "cso", "gcz", "rvz", "wia", "chd"):
        return _convert(path, ident, rep, progress, cancel, opts)
    if c == "ecm":
        return _ecm_job(path, ident, rep, progress, cancel, opts)
    if c == "" and ident.platform == "xbox" and ident.method == "dvd":
        try:
            with F.open_reader(path) as r:
                if X.xbe_needs_patch(r):
                    ident.container = "xbox-unpatched"
        except (OSError, F.FormatError):
            pass
        c = ident.container
    if c in ("xbox-redump", "xbox-untrimmed", "xbox-unpatched"):
        out_dir, done, lock = conversion_dir(path)
        iso = os.path.join(out_dir, "game.xiso")
        patched = 0
        try:
            if not done:
                ensure_space(out_dir, ident.size)
                with F.open_reader(path) as r:
                    _size, patched = X.rewrite(r, iso + ".part", lambda f: rep("Building the Xbox disc…", f), cancel)
                os.replace(iso + ".part", iso)
                with open(os.path.join(out_dir, "patched"), "w") as f:
                    f.write(str(patched))
                _done(out_dir)
            else:
                patched = int(_read_text(os.path.join(out_dir, "patched")) or 0)
            job = _converted(iso, path, ident, progress, cancel, opts)
        except BaseException:
            lock.release()
            raise
        job.locks.append(lock)
        job.details.append(("Prepared as", "trimmed XISO"))
        if patched:
            job.details.append(("Media patch", f"applied to {patched} program{'s' * (patched != 1)}"))
        return job
    if c == "xbox-folder":
        folder = ident.source if os.path.isdir(ident.source) else os.path.dirname(path)
        out_dir = work_dir("xbox")
        iso = os.path.join(out_dir, "game.xiso")
        try:
            ensure_space(out_dir, ident.size)
            _size, patched = X.folder_to_xiso(folder, iso, lambda f: rep("Building the Xbox disc…", f), cancel)
            job = _converted(iso, folder, ident, progress, cancel, opts)
        except BaseException:
            shutil.rmtree(out_dir, ignore_errors=True)
            raise
        job.extra.append(out_dir)
        if patched:
            job.details.append(("Media patch", f"applied to {patched} program{'s' * (patched != 1)}"))
        return job
    if ident.method == "cd":
        return _cd_job(path, ident, rep, opts or {})
    if ident.method == "cd-iso":
        return _iso_cd_job(path, ident)
    if ident.method == "dvd":
        return _dvd_job(path, ident)
    return _blocked(ident)


def _read_text(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _convert(path: str, ident: D.Identity, rep, progress, cancel, opts) -> Job:
    """WBFS / CSO / ZSO / GCZ / RVZ / WIA / CHD -> a plain image in a locked cache folder."""
    c = ident.container
    out_dir, done, lock = conversion_dir(path)
    try:
        if not done:
            ensure_space(out_dir, ident.size)
            step = lambda f: rep("Unpacking the game…", f)  # noqa: E731
            if c in ("wbfs", "cso", "gcz"):
                with F.open_reader(path) as r:
                    r.copy_to(os.path.join(out_dir, "game.iso.part"), step, cancel)
                os.replace(os.path.join(out_dir, "game.iso.part"), os.path.join(out_dir, "game.iso"))
            elif c in ("rvz", "wia"):
                F.rvz_to_iso(path, os.path.join(out_dir, "game.iso.part"), step, cancel)
                os.replace(os.path.join(out_dir, "game.iso.part"), os.path.join(out_dir, "game.iso"))
            else:
                F.chd_extract(path, out_dir, step, cancel)
            _done(out_dir)
        inner = next((os.path.join(out_dir, n) for n in sorted(os.listdir(out_dir))
                      if n.endswith((".cue", ".iso"))), None)
        if not inner:
            raise JobError("The game couldn't be unpacked (no disc image came out).")
        job = _converted(inner, path, ident, progress, cancel, opts)
    except BaseException:
        lock.release()
        raise
    job.locks.append(lock)
    return job


def _ecm_job(path: str, ident: D.Identity, rep, progress, cancel, opts) -> Job:
    """An .ecm opened directly: decode it (and route its .cue, if there is one, to the result)."""
    cue = D.find_cue_for(path[:-4]) or D.find_cue_for(path)
    if cue:
        return open_job(cue, progress=progress, cancel=cancel, opts=opts)
    out_dir, done, lock = conversion_dir(path)
    name = os.path.basename(path)[:-4]
    out = os.path.join(out_dir, name if name.lower().endswith((".bin", ".img", ".iso")) else name + ".bin")
    try:
        if not done:
            ensure_space(out_dir, ident.size * 3)
            F.ecm_decode(path, out + ".part", lambda f: rep("Unpacking the game…", f), cancel)
            os.replace(out + ".part", out)
            _done(out_dir)
        job = _converted(out, path, ident, progress, cancel, opts)
    except BaseException:
        lock.release()
        raise
    job.locks.append(lock)
    return job


def _blocked(ident: D.Identity) -> Job:
    job = Job(ident, ident.method or "none", ident.title, ident.source, ident.media, ident.sectors)
    job.blockers = list(ident.blockers) or ["DiscForge doesn't know how to burn this file."]
    job.warnings = list(ident.warnings)
    return job


def _converted(image: str, source: str, outer: D.Identity, progress, cancel, opts) -> Job:
    job = open_job(image, progress=progress, cancel=cancel, opts=opts)
    job.title = outer.title or job.title
    if outer.game_id and not job.game_id:
        job.game_id = outer.game_id
    job.source = source
    job.warnings = list(dict.fromkeys(outer.warnings + job.warnings))
    if outer.container and outer.container != "xbox-unpatched":
        job.details.insert(0, ("Original", outer.container.upper().replace("XBOX-", "Xbox ")))
    return job


def _rewrite_cue(cue: str, out_dir: str) -> str:
    """Copy a CUE into out_dir: decoded files are already there, the others are linked in, so every
    FILE line names a file next to the new CUE."""
    src_dir = os.path.dirname(os.path.abspath(cue))
    lines = []
    with open(cue, encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            m = re.match(r'(\s*FILE\s+)"([^"]+)"(.*)', line, re.I) or re.match(r"(\s*FILE\s+)(\S+)(.*)", line, re.I)
            if m:
                base = os.path.basename(m.group(2).replace("\\", "/"))
                if not os.path.exists(os.path.join(out_dir, base)):
                    src = _find_next_to(src_dir, base)
                    if src is None:
                        raise JobError(f"A file of this game is missing: {base}")
                    CUE._link(out_dir, src, base)
                line = f'{m.group(1)}"{base}"{m.group(3).rstrip()}\n'
            lines.append(line)
    out = os.path.join(out_dir, os.path.basename(cue))
    with open(out, "w") as f:
        f.writelines(lines)
    return out


def _base_job(ident: D.Identity, method: str) -> Job:
    job = Job(ident, method, ident.title, ident.source, ident.media, ident.sectors, game_id=ident.game_id)
    job.warnings = list(ident.warnings)
    job.blockers = list(ident.blockers)
    job.details = list(ident.details)
    return job


def check_toc(toc: str, layout: list[tuple[int, int, bool]], frames: int, cwd: str | None = None) -> str | None:
    """Let cdrdao itself read the TOC: a syntax problem or a track position that differs from DiscForge's
    plan is caught now, not halfway through a burn.  Returns the problem, or None."""
    exe = tools.find("cdrdao")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "show-toc", toc], capture_output=True, text=True, errors="replace", timeout=60,
                           stdin=subprocess.DEVNULL, cwd=cwd or os.path.dirname(os.path.abspath(toc)),
                           env=tools.env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = r.stdout + r.stderr
    if r.returncode != 0:
        err = next((ln.strip() for ln in out.splitlines() if "ERROR" in ln), out.strip()[-200:])
        return f"The burning tool can't use this disc layout: {err}"
    starts = [int(m) for m in re.findall(r"^\s*START\s+\S+\(\s*(\d+)\)", out, re.M)]
    ends = [int(m) for m in re.findall(r"^\s*END\s+\S+\(\s*(\d+)\)", out, re.M)]
    if layout and starts and starts != [lba for _n, lba, _d in layout]:
        return "The disc layout came out differently than planned, so DiscForge stopped before burning."
    if frames and ends and ends[-1] != frames:
        return "The disc size came out differently than planned, so DiscForge stopped before burning."
    return None


def _cd_job(path: str, ident: D.Identity, rep, opts: dict) -> Job:
    sheet = ident.cue
    if sheet is None:            # a cdrdao .toc: burned as written, from its own folder
        job = _base_job(ident, "cd")
        job.cd = CdPlan(path, False, True, 0, [], title=ident.title, speed=ident.plat.speed,
                        cwd=os.path.dirname(path), platform=ident.platform)
        job.warnings.append("cdrdao TOC files are burned as written, so the disc's contents can't be compared "
                            "with the game afterwards.")
        return job
    missing = [f for f in sheet.files if not os.path.exists(f)]
    if missing:
        raise JobError(f"A file of this game is missing: {os.path.basename(missing[0])}")
    work = work_dir("cd")
    try:
        lc_notes: list[tuple[str, str]] = []
        lc_warn: list[str] = []
        notes: list[tuple[str, str]] = []
        if any(t.sector_size == 2336 for t in sheet.tracks):
            ensure_space(work, sum(os.path.getsize(t.file) for t in sheet.tracks if t.sector_size == 2336) * 2)
            sheet = CUE.expand_2336(sheet, work, lambda f: rep("Rebuilding full CD sectors…", f))
            notes.append(("Sectors", "2336-byte sectors rebuilt as full raw sectors"))
        if ident.platform == "ps1":
            sheet, lc_notes, lc_warn = _libcrypt(sheet, work, rep, opts.get("libcrypt", True))
        toc = os.path.join(work, "disc.toc")
        swap = CUE.write_toc(sheet, toc)
        steps, frames = CUE.plan(sheet)
        layout = [(p.track.number, p.lba, not p.track.is_audio) for p in steps]
        verify = []
        for p in steps:
            t = p.track
            if not t.is_audio:
                verify.append(VerifyRange(p.lba, t.body, t.file, t.indexes[1] * t.sector_size, t.sector_size))
        job = _base_job(ident, "cd")
        job.work = work
        job.sectors = frames
        job.cd = CdPlan(toc, swap, all(t.sector_size == 2352 for t in sheet.tracks), frames, layout, verify,
                        ident.title, ident.plat.speed, raw_only=ident.raw_only, platform=ident.platform)
        if any(t.sector_size not in (2352, 2048) for t in sheet.tracks):
            job.blockers.append("This CUE uses a track format that can't be burned exactly.")
        problem = check_toc(toc, layout, frames)
        if problem:
            job.blockers.append(problem)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    job.details += notes + lc_notes
    job.warnings += lc_warn
    if (ident.platform == "ps1" and not lc_notes and not lc_warn
            and os.path.exists(os.path.splitext(path)[0] + ".sbi")):
        job.warnings.append("This PAL game comes with LibCrypt protection data (.sbi) but isn't in the LibCrypt "
                            "patch list, so a burned copy may stop working partway through.")
    return job


def _libcrypt(sheet, work: str, rep, enabled: bool):
    """Patch LibCrypt out of a PAL PlayStation game (on a copy).  Returns (sheet, details, warnings)."""
    from . import libcrypt as LC
    boot = CUE.ps_boot_file(sheet)
    entry = LC.lookup(boot) if boot else None
    if not entry:
        return sheet, [], []
    data = sheet.data_tracks[0]
    if not enabled:
        return sheet, [], [f"{entry['title'].split(' / ')[0]} has LibCrypt copy protection and automatic "
                           "patching is turned off, so a burned copy may stop working partway through."]
    if data.file_type != "BINARY" or data.sector_size != 2352:
        return sheet, [], ["This LibCrypt-protected game isn't a raw .bin image, so it can't be patched."]
    rep("Removing LibCrypt copy protection…", -1)
    out = os.path.join(work, "libcrypt-patched.bin")
    res = LC.patch_image(data.file, out, boot, progress=lambda f: rep("Removing LibCrypt copy protection…", f))
    if res.status == "patched":
        import copy
        sheet = copy.deepcopy(sheet)
        for t in sheet.tracks:
            if t.file == data.file:
                t.file = out
        return sheet, [("LibCrypt", "protection removed automatically")], []
    if res.status == "already":
        return sheet, [("LibCrypt", "already removed from this image")], []
    if res.status == "unprotected":
        return sheet, [("LibCrypt", "this version has no protection")], []
    why = " ".join(res.notes) or "The LibCrypt patch doesn't fit this copy."
    return sheet, [], [f"This game has LibCrypt copy protection. {why} A burned copy may stop working "
                       "partway through; a Redump-verified image usually fixes that."]


def _iso_cd_job(path: str, ident: D.Identity) -> Job:
    """A 2048-byte ISO burned to CD: Mode 1 (PC, Saturn, Sega CD) or Mode 2 Form 1 (PlayStation)."""
    work = work_dir("cd")
    try:
        sectors = ident.sectors
        link = CUE._link(work, path, "data.iso")
        mode = ident.cd_mode or "MODE1"
        lines = ["CD_ROM_XA" if mode.startswith("MODE2") else "CD_ROM", "", f"TRACK {mode}",
                 f'DATAFILE "{link}" {CUE.frames_to_msf(sectors)}']
        if sectors < 300:
            lines.append(f"ZERO {CUE.frames_to_msf(300 - sectors)}")
        toc = os.path.join(work, "disc.toc")
        with open(toc, "w") as f:
            f.write("\n".join(lines) + "\n")
        job = _base_job(ident, "cd")
        job.work = work
        job.sectors = max(sectors, 300)
        layout = [(1, 0, True)]
        job.cd = CdPlan(toc, False, False, job.sectors, layout, [VerifyRange(0, sectors, path, 0, 2048)],
                        ident.title, ident.plat.speed, platform=ident.platform)
        problem = check_toc(toc, layout, job.sectors)
        if problem:
            job.blockers.append(problem)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return job


def _dvd_job(path: str, ident: D.Identity) -> Job:
    job = _base_job(ident, "dvd")
    job.sectors = -(-os.path.getsize(path) // 2048)
    job.media = P.media_for(job.sectors, "dvd")
    job.dvd = DvdPlan(path, job.sectors, job.media, ident.layer_break, ident.title, ident.plat.speed,
                      booktype=ident.platform != "pc", plus_dl_only=ident.plus_dl_only, platform=ident.platform)
    if job.sectors > P.DVD9:
        job.blockers.append("This image is larger than a dual-layer DVD.")
    if job.media == "dvd" and job.sectors > P.DVD5:
        job.details.append(("Disc", "DVD-R (slightly too big for DVD+R)"))
    return job


def _folder_job(path: str, ident: D.Identity, rep, cancel, opts) -> Job:
    from .loader import _has_boot_program
    inner = pick_game(path)                 # a game image in the folder comes first
    if inner:
        return open_job(inner, progress=lambda t, f: rep(t, f), cancel=cancel, opts=opts)
    if _has_boot_program(path):
        ident.method = "dc"
        return _dc_job(path, ident, rep, opts)
    # anything else: a plain data disc of the folder's files
    if not any(fs for _r, _d, fs in os.walk(path)):
        raise JobError("This folder is empty, so there's nothing to burn.")
    mkisofs = tools.find("mkisofs") or tools.find("genisoimage")
    if not mkisofs:
        raise JobError("Making a data disc needs mkisofs, which isn't installed.")
    total = sum(os.path.getsize(os.path.join(r, f)) for r, _d, fs in os.walk(path) for f in fs)
    work = work_dir("data")
    try:
        ensure_space(work, total)
        iso = os.path.join(work, "data.iso")
        label = re.sub(r"[^A-Z0-9_]", "_", ident.title.upper())[:32] or "DISC"
        rep("Building the data disc…", -1)
        args = [mkisofs, "-quiet", "-V", label, "-J", "-joliet-long", "-r", "-l", "-iso-level", "3", "-udf"
                if total > 4 << 30 else "-D", "-o", iso, path]
        r = subprocess.run(args, capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL,
                           env=tools.env())
        if r.returncode != 0 or not os.path.exists(iso):
            raise JobError("Couldn't build the data disc: " + (r.stderr.strip().splitlines() or ["?"])[-1])
        di = D.identify(iso)
        di.platform, di.title, di.source = "pc", ident.title, path
        job = _iso_cd_job(iso, di) if di.method == "cd-iso" else _dvd_job(iso, di)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    job.extra.append(work)
    job.details.append(("Contents", f"{sum(len(fs) for _r, _d, fs in os.walk(path)):,} files"))
    return job


def _dc_job(path: str, ident: D.Identity, rep, opts: dict | None) -> Job:
    from .library import quick_info
    from .loader import load_any
    from .plan import make_plan
    opts = dict(opts or {})
    opts.pop("libcrypt", None)
    region_free = opts.pop("region_free", True)
    vga = opts.pop("vga", True)
    capacity = opts.pop("capacity", None)
    disc = load_any(path, progress=lambda t, f: rep(t, f), **opts)
    kw = {"capacity": capacity} if capacity else {}
    plan = make_plan(disc, region_free=region_free, vga=vga, **kw)
    title = quick_info(path)[0] or ident.title
    info = disc.ip() if hasattr(disc, "ip") else None
    if info is not None and getattr(info, "valid", False) and info.title.strip() and disc.kind != "homebrew":
        from .library import smart_case
        title = smart_case(info.title.strip())
    job = Job(ident, "dc", title, path, "cd", plan.end_lba, disc=disc, plan=plan)
    job.ident.platform = "dreamcast"
    job.warnings = list(plan.warnings) + [w for w in disc.warnings if w not in plan.warnings]
    job.blockers = list(plan.errors)
    if info is not None and getattr(info, "valid", False):
        job.game_id = (info.product_no or "").strip()
    job.details.append(("Disc", {"cdi": "DiscJuggler image", "gdi": "GD-ROM dump (converted)",
                                 "iso": "ISO image", "homebrew": "Homebrew"}.get(disc.kind, disc.kind)))
    return job


# --------------------------------------------------------------------------

def burn_job(job: Job, opts: BurnOptions, log: Callable[[str], None] = print,
             events: Callable[[dict], None] | None = None, holder: list | None = None) -> BurnResult:
    """Burn a prepared job.  `holder`, if given, receives the burner object (for cancel())."""
    if job.blockers:
        raise BurnError(job.blockers[0])
    if job.method == "dc":
        b = Burner(opts, log=log, events=events)
        if holder is not None:
            holder.append(b)
        return b.burn(job.plan)
    if job.method == "cd":
        b = CdrdaoBurner(opts, log=log, events=events)
        if holder is not None:
            holder.append(b)
        return b.burn(job.cd)
    if job.method == "dvd":
        b = dvd_burner(opts, log=log, events=events)
        if holder is not None:
            holder.append(b)
        return b.burn(job.dvd)
    raise BurnError("Nothing to burn.")
