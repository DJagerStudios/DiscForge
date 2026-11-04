"""CD burning disc-at-once with cdrdao, then an exact read-back check.

Console CDs (PlayStation, Saturn, Sega CD, PC Engine CD, 3DO, CD-i, PS2 CD games ...) are written raw
(generic-mmc-raw): every 2352-byte sector goes to the disc exactly as it is in the image - headers,
subheaders, Form 2 sectors, EDC/ECC - with the image's own track layout and gaps.  Drives that can't
write raw fall back to cdrdao's cooked DAO driver, except for games that only work raw.

Verify: the disc's TOC must match the planned layout track for track, then every data sector is
read back with READ CD and compared byte for byte (whole raw sectors for raw tracks, user data for
ISO tracks).  Audio isn't compared (drives read audio with a sample offset).  The result is True only
when every data sector was compared; a partial or impossible check gives None plus an explanation.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
from dataclasses import dataclass, field

from . import burner as B
from . import devices, tools
from .burner import BurnError, BurnResult
from .engine import RawCd, ToolBurner, media_ok, tool, wait_ready


@dataclass
class VerifyRange:
    lba: int                 # first sector on the disc
    count: int
    file: str
    offset: int              # byte offset of that sector in the file
    sector_size: int         # 2352 (raw track) or 2048 (ISO track)


@dataclass
class CdPlan:
    toc: str
    swap: bool
    raw: bool                                    # generic-mmc-raw (exact sectors) vs. cooked
    frames: int                                  # program-area sectors
    layout: list[tuple[int, int, bool]]          # (track, INDEX 01 LBA, is data)
    verify: list[VerifyRange] = field(default_factory=list)
    title: str = ""
    speed: int = 4                               # preferred speed for this console
    raw_only: bool = False                       # never fall back to cooked writing
    platform: str = ""
    cwd: str | None = None                       # folder cdrdao runs in (user TOCs with relative names)


ERRORS = [
    (r"not empty and not appendable|disk seems to be written", "The disc isn't blank.",
     "Insert a blank CD-R."),
    (r"exceeds capacity", "The game doesn't fit on this CD-R.", "Use an 80-minute (700 MB) CD-R."),
    (r"power calibration failed", "The drive couldn't tune its laser for this disc.",
     "Try a CD-R from a different brand (Verbatim discs are a safe bet)."),
    (r"medium not present|no disk", "There's no disc in the drive.",
     "Insert a blank CD-R and try again."),
    (r"write data failed|write error|medium error|write failed|sense key", "The drive reported a write error.",
     "Try a fresh blank CD-R, ideally another brand, at a slower speed."),
    (r"busy", "Another program is using the drive.", "Close other disc or music apps and try again."),
    (r"permission denied|cannot open", "This account isn't allowed to use the drive.",
     "Use “Allow access” on the Drives page, then log out and back in."),
    (r"buffer under", "The computer couldn't send data fast enough.", "Close heavy programs and try again."),
    (r"cannot open (data|audio) file", "A file of the game couldn't be opened.",
     "Make sure the game's files are all there and readable."),
]
RAW_UNSUPPORTED = re.compile(r"cannot setup disk-at-once|sub-channel writing mode|raw .*not supported|"
                             r"illegal (mode|field|request)|cannot setup write parameters|"
                             r"invalid field in cdb|write mode page", re.I)
TRANSIENT = re.compile(r"not ready|becoming ready|busy|unit attention|reset occurred", re.I)


def humanize(lines: list[str]) -> tuple[str, str]:
    text = "\n".join(lines).lower()
    for rx, why, fix in ERRORS:
        if re.search(rx, text):
            return why, fix
    return "The burn stopped unexpectedly.", "Try again with a fresh blank CD-R."


def cdrdao_version(exe: str) -> tuple[int, ...]:
    """cdrdao's version (it prints it on any call); (0,) when unknown."""
    try:
        r = subprocess.run([exe], capture_output=True, text=True, errors="replace", timeout=20,
                           stdin=subprocess.DEVNULL, env=tools.env())
    except (OSError, subprocess.TimeoutExpired):
        return (0,)
    m = re.search(r"version\s+(\d+)\.(\d+)\.(\d+)", r.stdout + r.stderr, re.I)
    return tuple(int(x) for x in m.groups()) if m else (0,)


class CdrdaoBurner(ToolBurner):
    stop_signal = getattr(signal, "SIGQUIT", signal.SIGTERM)   # cdrdao aborts cleanly on QUIT

    def burn(self, plan: CdPlan) -> BurnResult:
        dev = self.opts.dev
        cdrdao = tool("cdrdao", "cdrdao")
        self.events({"stage": "prepare", "frac": 0.0})
        if plan.swap and plan.raw and (0,) < cdrdao_version(cdrdao) < (1, 2, 5):
            raise BurnError("The installed cdrdao is older than 1.2.5, which turns CD audio into noise when "
                            "writing raw. Update cdrdao (or use DiscForge's bundled one) and try again.")
        m = B.media_info(dev)
        why = media_ok(m, "cd", plan.frames, plan.platform)
        if why:
            raise BurnError(why)
        if not m.blank:
            raise BurnError("The disc isn't blank. Insert a blank CD-R.")
        self.log(f"Disc: {m.kind or 'CD-R'}, blank, {m.capacity or '?'} sectors free; image needs {plan.frames}.")
        if m.capacity and plan.frames > m.capacity:
            raise BurnError("The game doesn't fit on this CD-R. Use an 80-minute (700 MB) disc.")
        res = BurnResult(ok=False, simulated=self.opts.simulate)
        devices.prepare_for_burn(dev, self.log)
        with B.SleepInhibitor(f"Burning {plan.title or 'a disc'}"):
            raw = plan.raw
            for attempt in range(4):
                rc, tail, started = self._write(cdrdao, plan, raw)
                if rc == 0:
                    break
                self.check_cancel()
                text = "\n".join(tail)
                if not started and raw and RAW_UNSUPPORTED.search(text):
                    if plan.raw_only:
                        raise BurnError("This game only works when the disc is written raw, and this drive can't "
                                        "write raw sectors. Try another burner (most DVD writers can). "
                                        "(Nothing was written.)")
                    self.log("This drive can't write raw sectors; switching to its standard disc-at-once mode.")
                    raw = False
                    continue
                if not started and attempt < 2 and TRANSIENT.search(text):
                    self.log("The drive isn't ready yet; trying again in a few seconds…")
                    self.sleep(6)
                    continue
                why, fix = humanize(tail)
                raise BurnError(f"{why} {fix}" + ("" if started else " (Nothing was written.)"))
            else:
                raise BurnError("The drive kept refusing to start. Re-insert the disc and try again.")
            self.written = True
            res.ok = True
            if self.opts.simulate:
                res.messages.append("Practice run finished - nothing was written.")
                return res
            if self.opts.verify:
                res.verified, note = self.verify(plan, raw)
                if note:
                    res.messages.append(note)
                if res.verified is False:
                    res.ok = False
        if self.opts.eject:
            B.eject(dev)
        return res

    def _write(self, cdrdao: str, plan: CdPlan, raw: bool) -> tuple[int, list[str], bool]:
        speed = plan.speed if self.opts.speed is None else self.opts.speed
        args = [cdrdao, "simulate" if self.opts.simulate else "write", "--device",
                devices.address(self.opts.dev, "cdrdao"), "--driver", "generic-mmc-raw" if raw else "generic-mmc",
                "-n", "-v", "2"]
        if speed:
            args += ["--speed", str(speed)]
        if plan.swap:
            args.append("--swap")
        args.append(plan.toc)
        state = {"started": False, "total": max(1, plan.frames * 2352 >> 20)}

        def on_line(line: str) -> None:
            low = line.lower()
            if m := re.search(r"Wrote (\d+) of (\d+) MB", line):
                state["started"] = True
                self.writing = True
                done, total = int(m.group(1)), max(1, int(m.group(2)))
                self.events({"stage": "write", "frac": min(1.0, done / total), "mb": done, "mb_total": total,
                             "speed": self.speed})
                return
            if m := re.search(r"at speed (\d+)", line):
                self.speed = float(m.group(1))
            if "writing lead-in" in low:
                state["started"] = True
                self.writing = True
                self.events({"stage": "write", "frac": 0.0, "mb": 0, "mb_total": state["total"], "speed": self.speed,
                             "note": "lead-in"})
            elif "writing lead-out" in low or "flushing cache" in low:
                self.events({"stage": "fixate", "frac": 1.0})
            if not re.match(r"\s*(Wrote|Track\s+\d|\d+:\d+)", line):
                self.log(line)

        rc, tail = self.run(args, on_line, cwd=plan.cwd or os.path.dirname(os.path.abspath(plan.toc)))
        self.writing = False
        return rc, tail, state["started"]

    # -- verify --------------------------------------------------------------
    def verify(self, plan: CdPlan, raw: bool = True) -> tuple[bool | None, str]:
        """(True = every data sector matches | None = couldn't fully check | False = differs, note)."""
        dev = self.opts.dev
        self.events({"stage": "verify", "frac": 1.0, "part": 0.0})
        self.log("Verifying the burned disc…")
        if not plan.layout:
            return None, "This disc was burned from a TOC file, so it couldn't be compared with the game."
        wait_ready(dev, self.log)
        toc, err = None, None
        for _ in range(10):
            try:
                toc = B.read_toc(dev)
                break
            except OSError as e:
                err = e
                self.sleep(2)
        if toc is None:
            self.log(f"Verify FAILED: couldn't read the disc's table of contents ({err}).")
            return False, "The disc's table of contents couldn't be read back."
        got = [(n, lba) for n, lba, _d in toc if n != 0xAA]
        want = [(n, lba) for n, lba, _d in plan.layout]
        self.log("TOC: " + ", ".join(f"{n}@{lba}" for n, lba in got))
        if got != want:
            self.log("Verify FAILED: the tracks on the disc aren't where the image puts them "
                     f"(expected {', '.join(f'{n}@{lba}' for n, lba in want)}).")
            return False, "The tracks on the disc aren't where the game expects them."
        total = sum(v.count for v in plan.verify)
        if not total:
            self.log("Track layout matches (audio-only disc: audio can't be compared exactly).")
            return None, "The track layout matches; CD audio can't be compared exactly, so it wasn't checked."
        try:
            rd = RawCd(dev)
        except OSError as e:
            self.log(f"Raw reads unavailable ({e}); checking the data through the drive's normal reads.")
            return self.verify_cooked(plan)
        done = bad = 0
        first_bad = None
        try:
            for v in plan.verify:
                with open(v.file, "rb") as f:
                    pos = 0
                    while pos < v.count:
                        self.check_cancel()
                        n = min(24, v.count - pos)
                        f.seek(v.offset + pos * v.sector_size)
                        want_b = f.read(n * v.sector_size)
                        try:
                            got_b = rd.read(v.lba + pos, n, user_only=v.sector_size == 2048)
                        except OSError as e:
                            if done == 0:
                                self.log(f"Raw reads failed ({e}); checking through normal reads instead.")
                                rd.close()
                                rd = None
                                return self.verify_cooked(plan)
                            self.log(f"Verify FAILED: read error at sector {v.lba + pos} ({e}).")
                            return False, f"Sector {v.lba + pos} couldn't be read back."
                        if got_b != want_b:
                            ss = v.sector_size
                            for k in range(n):
                                if got_b[k * ss:(k + 1) * ss] != want_b[k * ss:(k + 1) * ss]:
                                    bad += 1
                                    first_bad = first_bad if first_bad is not None else v.lba + pos + k
                        pos += n
                        done += n
                        if done % 480 < 24 or done == total:
                            self.events({"stage": "verify", "frac": 1.0, "part": done / total})
        finally:
            if rd is not None:
                rd.close()
        if bad:
            self.log(f"Verify FAILED: {bad} sector(s) differ from the image (first at sector {first_bad}).")
            return False, f"{bad:,} sector(s) differ from the game."
        self.log(f"Verify OK: track layout and all {total:,} data sectors match the image exactly.")
        return True, ""

    def verify_cooked(self, plan: CdPlan) -> tuple[bool | None, str]:
        """Fallback: compare the 2048-byte user data of Mode 1 / Form 1 sectors through readcd."""
        readcd = tools.find("readcd") or tools.find("readom")
        if not readcd:
            self.log("Verify skipped: readcd isn't installed; only the track layout was checked.")
            return None, "Only the disc's track layout could be checked (readcd isn't available)."
        runs = []
        for v in plan.verify:
            runs += _form1_runs(v)
        total = sum(r[1] for r in runs) or 1
        all_sectors = sum(v.count for v in plan.verify)
        done = 0
        addr = devices.address(self.opts.dev, "readcd")
        for lba, count, file, offsets in runs:
            proc = subprocess.Popen([readcd, f"dev={addr}", f"sectors={lba}-{lba + count}", "f=-"],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                    env=tools.env({"LC_ALL": "C"}))
            self._proc = proc
            ok = True
            try:
                assert proc.stdout
                with open(file, "rb") as f:
                    for k in range(count):
                        got = proc.stdout.read(2048)
                        f.seek(offsets[k])
                        if got != f.read(2048):
                            self.log(f"Verify FAILED: sector {lba + k} differs from the image.")
                            ok = False
                            break
                        done += 1
                        if done % 512 == 0:
                            self.check_cancel()
                            self.events({"stage": "verify", "frac": 1.0, "part": done / total})
            finally:
                if proc.poll() is None and not ok:
                    proc.kill()
                if proc.stdout:
                    proc.stdout.close()
                rc = proc.wait()
                self._proc = None
            if not ok:
                return False, f"Sector {lba + k} differs from the game."
            if rc != 0:
                self.log(f"Verify FAILED: couldn't read sectors {lba}-{lba + count}.")
                return False, f"Sectors {lba}-{lba + count} couldn't be read back."
        if done < all_sectors:
            self.log(f"Verify: {done:,} of {all_sectors:,} data sectors compared (the rest are streamed Form 2 "
                     "sectors this drive/OS can't read back exactly).")
            return None, (f"{done:,} of {all_sectors:,} data sectors could be compared and all matched; streamed "
                          "audio/video sectors can't be read back exactly on this system.")
        self.log(f"Verify OK: track layout and {done:,} data sectors match the image.")
        return True, ""


def _form1_runs(v: VerifyRange) -> list[tuple[int, int, str, list[int]]]:
    """Runs of sectors readable as 2048-byte data: (lba, count, file, byte offset of each user area)."""
    if v.sector_size == 2048:
        return [(v.lba, v.count, v.file, [v.offset + k * 2048 for k in range(v.count)])]
    runs: list[tuple[int, int, str, list[int]]] = []
    cur: list[int] = []
    start = None
    with open(v.file, "rb") as f:
        for k in range(v.count):
            f.seek(v.offset + k * 2352 + 15)
            hdr = f.read(4)               # mode byte + subheader file/channel/submode
            if len(hdr) < 4:
                break
            mode, submode = hdr[0], hdr[3]
            user = None
            if mode == 1:
                user = v.offset + k * 2352 + 16
            elif mode == 2 and not submode & 0x20:
                user = v.offset + k * 2352 + 24
            if user is None:
                if cur:
                    runs.append((start, len(cur), v.file, cur))
                cur, start = [], None
                continue
            if start is None:
                start = v.lba + k
            cur.append(user)
    if cur:
        runs.append((start, len(cur), v.file, cur))
    return runs
