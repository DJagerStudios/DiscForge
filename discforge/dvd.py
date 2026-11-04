"""DVD burning (PS2, GameCube, Wii, Xbox, big PC images), then a full read-back.

Linux / FreeBSD: growisofs -dvd-compat closes the disc (maximum DVD-ROM compatibility); DVD-R(W) is
written disc-at-once; on DVD+R DL the image's own layer break is set (growisofs can't set one on
DVD-R DL, so games that need an exact break ask for DVD+R DL).  On DVD+R the book type is set to
DVD-ROM first when the drive allows it, which helps console drives accept the disc.
Windows / macOS: cdrecord in DAO mode (growisofs isn't available there), layerbreak=N for DL.
Verify compares every sector of the disc with the image.
"""
from __future__ import annotations

import mmap
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

from . import burner as B
from . import devices, tools
from .burner import BurnError, BurnResult
from .engine import ToolBurner, media_ok, tool, wait_ready
from .platforms import DVD5


@dataclass
class DvdPlan:
    image: str
    sectors: int                      # 2048-byte sectors
    media: str                        # dvd | dvd-dl
    layer_break: int | None = None
    title: str = ""
    speed: int = 4
    booktype: bool = True             # try DVD-ROM book type on DVD+R
    plus_dl_only: bool = False        # the layer break must be set exactly (DVD+R DL)
    platform: str = ""


def parse_mediainfo(out: str) -> dict:
    """dvd+rw-mediainfo: media type, status, free blocks and write speeds."""
    info: dict = {"kind": "", "blank": False, "free": None, "speeds": []}
    if m := re.search(r"Mounted Media:\s*\w+h,\s*(.+)", out):
        info["kind"] = m.group(1).strip()
    if m := re.search(r"Disc status:\s*(\w+)", out):
        info["blank"] = m.group(1) == "blank"
        info["status"] = m.group(1)
    if m := re.search(r"Free Blocks:\s*(\d+)\*2KB", out):
        info["free"] = int(m.group(1))
    info["speeds"] = sorted({float(s) for s in re.findall(r"Write Speed #\d+:\s*([\d.]+)x", out)})
    return info


def mediainfo(dev: str) -> dict:
    exe = tools.find("dvd+rw-mediainfo")
    if not exe:
        return {}
    try:
        r = subprocess.run([exe, devices.address(dev, "growisofs")], capture_output=True, text=True, errors="replace",
                           timeout=60, stdin=subprocess.DEVNULL, env=tools.env({"LC_ALL": "C"}))
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return parse_mediainfo(r.stdout + r.stderr)


def media_from_mediainfo(dev: str) -> B.Media | None:
    """The disc as dvd+rw-mediainfo sees it, for drives where cdrecord -minfo doesn't report DVDs."""
    mi = mediainfo(dev)
    if not mi.get("kind"):
        return None
    kind = mi["kind"].split()[0]
    if re.search(r"double|dual", mi["kind"], re.I) and "/DL" not in kind:
        kind += "/DL"
    return B.Media(present=True, kind=kind, blank=mi.get("blank", False), status=mi.get("status", ""),
                   erasable="RW" in kind, capacity=mi.get("free"))


def choose_dvd_speed(speeds: list[float], preferred: float) -> int:
    ok = [s for s in speeds if s >= preferred - 0.01]
    s = ok[0] if ok else (speeds[-1] if speeds else preferred)
    return max(1, int(round(s)))


ERRORS = [
    (r"is not appendable|media is not appendable|already carries", "The disc isn't blank.", "Insert a blank DVD."),
    (r"blocks are free|no space|exceeds|too large", "The game doesn't fit on this disc.",
     "Use a bigger (dual-layer) disc."),
    (r"power calibration|opc", "The drive couldn't tune its laser for this disc.", "Try a DVD from a different brand."),
    (r"write failed|medium error|write error|sense key|input/output error", "The drive reported a write error.",
     "Try a fresh blank DVD, ideally another brand, at a slower speed."),
    (r"busy|is mounted|exclusive access", "Another program is using the drive.",
     "Close other disc apps (and on a Mac, click Ignore on the blank-disc dialog) and try again."),
    (r"permission denied|unable to open|insufficient privileges", "This account isn't allowed to use the drive.",
     "Use “Allow access” on the Drives page, then log out and back in."),
    (r"no media|medium not present", "There's no disc in the drive.", "Insert a blank DVD and try again."),
]
TRANSIENT = re.compile(r"not ready|becoming ready|unit attention|busy", re.I)


def humanize(lines: list[str]) -> tuple[str, str]:
    text = "\n".join(lines).lower()
    for rx, why, fix in ERRORS:
        if re.search(rx, text):
            return why, fix
    return "The burn stopped unexpectedly.", "Try again with a fresh blank DVD."


class _DvdBase(ToolBurner):
    """Media checks, the burn loop and the read-back shared by the growisofs and cdrecord backends."""

    def _media(self, plan: DvdPlan) -> tuple[B.Media, dict, str]:
        dev = self.opts.dev
        m = B.media_info(dev)
        mi = mediainfo(dev)
        if mi.get("kind") and not m.kind:
            m.kind = mi["kind"].split()[0]
        if not m.present and mi.get("kind"):
            m.present, m.blank = True, mi.get("blank", False)
        if m.capacity is None and mi.get("free"):
            m.capacity = mi["free"]
        kind = (mi.get("kind") or m.kind).upper()
        if ("DOUBLE" in kind or "DUAL" in kind) and "DL" not in m.kind.upper():
            m.kind += "/DL"
        why = media_ok(m, plan.media, plan.sectors, plan.platform, plan.plus_dl_only)
        if why:
            raise BurnError(why)
        rewritable = "RW" in kind
        if not m.blank and not rewritable:
            raise BurnError("The disc isn't blank. Insert a blank DVD.")
        free = m.capacity or mi.get("free")
        if free and plan.sectors > free and m.blank:
            raise BurnError(f"The game doesn't fit on this disc ({plan.sectors:,} sectors, {free:,} free).")
        self.log(f"Disc: {mi.get('kind') or m.kind}, {free or '?'} sectors free; image needs {plan.sectors:,}.")
        return m, mi, kind

    def _speed(self, plan: DvdPlan, mi: dict) -> int:
        if self.opts.speed is None:
            return choose_dvd_speed(mi.get("speeds", []), plan.speed)
        return self.opts.speed                                  # 0: as fast as the drive allows

    def burn(self, plan: DvdPlan) -> BurnResult:
        self.events({"stage": "prepare", "frac": 0.0})
        m, mi, kind = self._media(plan)
        args = self.command(plan, m, mi, kind)
        res = BurnResult(ok=False, simulated=self.opts.simulate)
        devices.prepare_for_burn(self.opts.dev, self.log)
        with B.SleepInhibitor(f"Burning {plan.title or 'a disc'}"):
            self.before_write(plan, kind)
            for attempt in range(3):
                rc, tail, started = self._write(args)
                if rc == 0:
                    break
                self.check_cancel()
                if not started and attempt < 2 and TRANSIENT.search("\n".join(tail)):
                    self.log("The drive isn't ready yet; trying again in a few seconds…")
                    self.sleep(6)
                    continue
                why, fix = humanize(tail)
                raise BurnError(f"{why} {fix}" + ("" if started else " (Nothing was written.)"))
            self.written = True
            res.ok = True
            if self.opts.simulate:
                res.messages.append("Practice run finished - nothing was written.")
                return res
            if self.opts.verify:
                res.verified, note = self.verify(plan)
                if note:
                    res.messages.append(note)
                if res.verified is False:
                    res.ok = False
        if self.opts.eject:
            B.eject(self.opts.dev)
        return res

    def before_write(self, plan: DvdPlan, kind: str) -> None:
        pass

    def command(self, plan: DvdPlan, m: B.Media, mi: dict, kind: str) -> list[str]:
        raise NotImplementedError

    def _write(self, args: list[str]) -> tuple[int, list[str], bool]:
        raise NotImplementedError

    # -- verify --------------------------------------------------------------
    def verify(self, plan: DvdPlan) -> tuple[bool | None, str]:
        dev = self.opts.dev
        self.events({"stage": "verify", "frac": 1.0, "part": 0.0})
        self.log("Verifying the burned disc…")
        wait_ready(dev, self.log)
        if sys.platform.startswith(("linux", "freebsd")):
            got = self._verify_blockdev(plan)
            if got is not None:
                return got
        return self._verify_readcd(plan)

    def _compare(self, plan: DvdPlan, read) -> tuple[bool | None, str] | None:
        """Stream the disc through read(pos, n) and compare with the image; None = reading isn't possible."""
        total = plan.sectors * 2048
        img_size = os.path.getsize(plan.image)
        chunk = 4 << 20
        with open(plan.image, "rb") as f:
            pos = 0
            while pos < total:
                self.check_cancel()
                n = min(chunk, total - pos)
                try:
                    got = read(pos, n)
                except OSError as e:
                    if pos == 0:
                        self.log(f"Direct reads unavailable ({e}).")
                        return None
                    self.log(f"Verify FAILED: read error at sector {pos // 2048} ({e}).")
                    return False, f"Sector {pos // 2048:,} couldn't be read back."
                if len(got) != n:
                    if pos == 0 and not got:
                        return None
                    self.log(f"Verify FAILED: the disc ends early (at sector {(pos + len(got)) // 2048}).")
                    return False, "The disc ends before the game does."
                want = f.read(n) if pos < img_size else b""
                want += bytes(n - len(want))
                if got != want:
                    for k in range(0, n, 2048):
                        if got[k:k + 2048] != want[k:k + 2048]:
                            self.log(f"Verify FAILED: sector {(pos + k) // 2048} differs from the image.")
                            return False, f"Sector {(pos + k) // 2048:,} differs from the game."
                pos += n
                self.events({"stage": "verify", "frac": 1.0, "part": pos / total})
        self.log(f"Verify OK: all {plan.sectors:,} sectors match the image.")
        return True, ""

    def _verify_blockdev(self, plan: DvdPlan) -> tuple[bool | None, str] | None:
        """Read the device directly, with O_DIRECT where possible (no page cache, so nothing stale
        from before the burn can be compared)."""
        direct = getattr(os, "O_DIRECT", 0)
        try:
            fd = os.open(self.opts.dev, os.O_RDONLY | direct)
        except OSError:
            direct = 0
            try:
                fd = os.open(self.opts.dev, os.O_RDONLY)
            except OSError:
                return None
        buf = mmap.mmap(-1, 4 << 20)                  # page-aligned, as O_DIRECT needs

        def read(pos: int, n: int) -> bytes:
            if not direct:
                return os.pread(fd, n, pos)
            view = memoryview(buf)[:n]
            try:
                got = os.preadv(fd, [view], pos)
                return bytes(view[:got])
            finally:
                view.release()

        try:
            return self._compare(plan, read)
        finally:
            buf.close()
            os.close(fd)

    def _verify_readcd(self, plan: DvdPlan) -> tuple[bool | None, str]:
        exe = tools.find("readcd") or tools.find("readom")
        if not exe:
            return None, "The disc couldn't be read back on this system (readcd isn't available)."
        proc = subprocess.Popen([exe, f"dev={devices.address(self.opts.dev, 'readcd')}", f"sectors=0-{plan.sectors}",
                                 "f=-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                env=tools.env({"LC_ALL": "C"}))
        self._proc = proc
        try:
            assert proc.stdout
            stream = proc.stdout

            def read(_pos: int, n: int) -> bytes:
                out = bytearray()
                while len(out) < n:
                    piece = stream.read(n - len(out))
                    if not piece:
                        break
                    out += piece
                return bytes(out)

            got = self._compare(plan, read)
        finally:
            if proc.poll() is None:
                proc.kill()
            if proc.stdout:
                proc.stdout.close()
            proc.wait()
            self._proc = None
        if got is None:
            return False, "The disc couldn't be read back."
        return got


class DvdBurner(_DvdBase):
    """growisofs (Linux, FreeBSD)."""

    def before_write(self, plan: DvdPlan, kind: str) -> None:
        if plan.booktype and kind.startswith("DVD+R") and not self.opts.simulate:
            exe = tools.find("dvd+rw-booktype")
            if not exe:
                return
            try:
                r = subprocess.run([exe, "-dvd-rom", "-media", devices.address(self.opts.dev, "growisofs")],
                                   capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                                   errors="replace", env=tools.env())
                self.log("Book type set to DVD-ROM." if r.returncode == 0
                         else "This drive can't change the book type (fine - most consoles don't mind).")
            except (OSError, subprocess.TimeoutExpired):
                pass

    def command(self, plan: DvdPlan, m: B.Media, mi: dict, kind: str) -> list[str]:
        gis = tool("growisofs", "dvd+rw-tools")
        dash = kind.startswith("DVD-R")
        rewritable = "RW" in kind
        if self.opts.simulate and not dash:
            raise BurnError("Practice runs only work on DVD-R discs (DVD+R has no test mode).")
        speed = self._speed(plan, mi)
        args = [gis, "-dvd-compat"]
        if speed:
            args.append(f"-speed={speed}")
        lim = shutil.which("prlimit")
        if lim:          # an unlocked ring buffer: mlockall + the 8 MB memlock limit would break it
            args = [lim, "--memlock=0:0", "--"] + args
        if dash and not rewritable:
            args.append("-use-the-force-luke=dao")
        dl = plan.sectors > DVD5 or "DL" in m.kind.upper()
        if dl and plan.layer_break and kind.startswith("DVD+R"):
            args.append(f"-use-the-force-luke=break:{plan.layer_break}")
        if self.opts.simulate:
            args.append("-use-the-force-luke=dummy")
        args += ["-Z", f"{devices.address(self.opts.dev, 'growisofs')}={plan.image}"]
        return args

    def _write(self, args: list[str]) -> tuple[int, list[str], bool]:
        state = {"started": False}

        def on_line(line: str) -> None:
            if m := re.search(r"(\d+)/(\d+)\s*\(\s*([\d.]+)%\)\s*@([\d.]+)x,\s*remaining\s*(\S+)", line):
                state["started"] = True
                self.writing = True
                done, total = int(m.group(1)), max(1, int(m.group(2)))
                self.speed = float(m.group(4)) or self.speed
                eta = None
                if mm := re.match(r"(\d+):(\d+)", m.group(5)):
                    eta = int(mm.group(1)) * 60 + int(mm.group(2))
                self.events({"stage": "write", "frac": min(1.0, done / total), "mb": done >> 20,
                             "mb_total": total >> 20, "speed": self.speed, "eta": eta})
                return
            low = line.lower()
            if "flushing cache" in low or "closing" in low or "writing lead-out" in low:
                self.events({"stage": "fixate", "frac": 1.0})
            if "writing" in low and "lead-in" in low:
                state["started"] = True
            self.log(line)

        rc, tail = self.run(args, on_line)
        self.writing = False
        return rc, tail, state["started"]


class CdrecordDvdBurner(_DvdBase):
    """cdrecord in DAO mode (Windows, macOS, or wherever growisofs isn't installed)."""

    def command(self, plan: DvdPlan, m: B.Media, mi: dict, kind: str) -> list[str]:
        exe = tool("cdrecord", "cdrtools")
        speed = self._speed(plan, mi)
        args = [exe, "-v", f"dev={devices.address(self.opts.dev, 'cdrecord')}", f"gracetime={B.GRACE_TIME}",
                "driveropts=burnfree", "-sao"]
        if speed:
            args.append(f"speed={speed}")
        if self.opts.simulate:
            args.append("-dummy")
        dl = plan.sectors > DVD5 or "DL" in m.kind.upper()
        if dl and plan.layer_break:
            args.append(f"layerbreak={plan.layer_break}")
        args.append(plan.image)
        return args

    def _write(self, args: list[str]) -> tuple[int, list[str], bool]:
        state = {"started": False}

        def on_line(line: str) -> None:
            if m := re.search(r"Track \d+:\s*(\d+) of\s*(\d+) MB written.*?([\d.]+)x\.", line):
                state["started"] = True
                self.writing = True
                done, total = int(m.group(1)), max(1, int(m.group(2)))
                self.speed = float(m.group(3)) or self.speed
                self.events({"stage": "write", "frac": min(1.0, done / total), "mb": done, "mb_total": total,
                             "speed": self.speed})
                return
            if re.search(r"Fixating", line):
                self.events({"stage": "fixate", "frac": 1.0})
            if re.search(r"Starting new track|Writing pregap|Starting to write", line):
                state["started"] = True
            if not B.HARMLESS.search(line):
                self.log(line)

        rc, tail = self.run(args, on_line)
        self.writing = False
        return rc, [ln for ln in tail if not B.HARMLESS.search(ln)], state["started"]


def dvd_burner(opts, log=print, events=None) -> _DvdBase:
    """growisofs where it exists (Linux/BSD), cdrecord elsewhere."""
    if not tools.DVD_WITH_CDRECORD and tools.find("growisofs"):
        return DvdBurner(opts, log=log, events=events)
    return CdrecordDvdBurner(opts, log=log, events=events)
