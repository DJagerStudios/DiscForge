"""Burn planning: exactly what cdrecord writes, session by session, in TAO mode.

Disc geometry (Orange Book, Track-At-Once):
  * every TAO track ends with 2 run-out blocks;
  * closing a session costs lead-out (6750 sectors for the first session,
    2250 afterwards) + next lead-in (4500) + first pregap (150).
So after a 300-sector audio track the next session starts at
300 + 2 + 6750 + 4500 + 150 = 11702, the classic Dreamcast "msinfo".

Images (CDI) record each session's last track *including* the 2 run-out
blocks (e.g. 302 audio sectors), so the planner sizes the last track of every
non-final session from the LBA the next session must land on, instead of
trusting the stored length.

The boot (last) data track gets 150 sectors of zero padding after its
filesystem, like mkisofs adds by default: some drives can't read the very last
sectors before TAO run-out blocks, and padding keeps every file clear of them.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from . import ipbin
from . import sectors as S
from .cdi import WriterTrack
from .disc import CD80_CAPACITY, Disc
from .util import fmt_mb

FIRST_LEADOUT = 6750
NEXT_LEADOUT = 2250
LEADIN = 4500
PREGAP = 150
RUNOUT = 2
MIN_TRACK = 300
FINAL_PAD = 150
NEARLY_FULL = 0.97


def session_gap(index: int) -> int:
    return (FIRST_LEADOUT if index == 0 else NEXT_LEADOUT) + LEADIN + PREGAP


def iso_sectors(src: S.SectorSource, lba: int) -> int | None:
    """Size of the ISO 9660 filesystem at the start of a data track, in sectors."""
    try:
        pvd = src.read(16, 1)
    except (IndexError, IOError):
        return None
    if pvd[0:6] != b"\x01CD001":
        return None
    vs = struct.unpack_from("<I", pvd, 80)[0]
    if 16 < vs <= src.count + FINAL_PAD:
        return vs                      # mkisofs -C stores the session's own size
    if lba < vs <= lba + src.count:
        return vs - lba                # some tools store the absolute end
    return None


@dataclass
class PlanTrack:
    mode: str
    lba: int
    source: S.SectorSource
    label: str
    image_sectors: int
    pad: int = 0                       # zero sectors appended after the image's data

    @property
    def sectors(self) -> int:
        return self.source.count

    @property
    def size_bytes(self) -> int:
        return self.sectors * self.source.sector_bytes


@dataclass
class PlanSession:
    index: int
    tracks: list[PlanTrack]
    final: bool

    @property
    def start_lba(self) -> int:
        return self.tracks[0].lba

    @property
    def end_lba(self) -> int:
        t = self.tracks[-1]
        return t.lba + t.sectors + RUNOUT


@dataclass
class BurnPlan:
    disc: Disc
    sessions: list[PlanSession]
    capacity: int = CD80_CAPACITY
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def boot(self) -> PlanTrack:
        return self.sessions[-1].tracks[0]

    @property
    def end_lba(self) -> int:
        return self.sessions[-1].end_lba

    @property
    def total_write_sectors(self) -> int:
        return sum(t.sectors for s in self.sessions for t in s.tracks)

    def describe(self) -> list[str]:
        out = []
        for s in self.sessions:
            for t in s.tracks:
                if t.mode == S.AUDIO:
                    secs = t.sectors / 75
                    size = f"{int(secs // 60)}:{secs % 60:05.2f} of audio"
                else:
                    size = fmt_mb(t.sectors)
                adj = t.sectors - t.image_sectors - t.pad
                bits = []
                if adj:
                    bits.append(f"{'+' if adj > 0 else ''}{adj} sectors vs image")
                if t.pad:
                    bits.append(f"+{t.pad} padding")
                extra = f" ({', '.join(bits)})" if bits else ""
                close = "close disc" if s.final else "leave open"
                out.append(f"Session {s.index + 1}: {S.MODE_NAMES[t.mode]:<9} {t.sectors:>7} sectors "
                           f"at LBA {t.lba:<6} {size}{extra}  [TAO, {close}]")
        return out

    def cdi_sessions(self) -> list[list[WriterTrack]]:
        """The burned disc as a CDI (what an image of the finished CD-R looks like)."""
        return [[WriterTrack(t.mode, t.lba, t.source, runout=RUNOUT if i == len(s.tracks) - 1 else 0)
                 for i, t in enumerate(s.tracks)] for s in self.sessions]


def _blank(src: S.SectorSource, start: int, n: int) -> bool:
    """True if every one of the n sectors from `start` is all zeros."""
    pos = start
    while pos < start + n:
        k = min(1024, start + n - pos)
        if any(src.read(pos, k)):
            return False
        pos += k
    return True


def make_plan(disc: Disc, *, region_free: bool = False, vga: bool = False,
              capacity: int = CD80_CAPACITY) -> BurnPlan:
    plan = BurnPlan(disc, [], capacity)
    plan.errors += disc.blockers
    nsess = len(disc.sessions)
    if nsess < 2:
        plan.errors.append("Single-session disc: a Dreamcast selfboot CD-R needs a second (boot) session.")
    cur = 0
    for k, sess in enumerate(disc.sessions):
        final = k == nsess - 1
        if len(sess) > 1:
            plan.warnings.append(f"Session {k + 1} has {len(sess)} tracks (CD audio). Track gaps on the CD-R may "
                                 "differ slightly from the image; the boot session position is verified after "
                                 "session 1 is written.")
        tracks: list[PlanTrack] = []
        lba = cur
        for i, t in enumerate(sess):
            src = t.source
            image_n = src.count
            if i == 0 and k > 0 and t.lba != cur:
                if final and disc.rebuild is not None:
                    src = disc.rebuild(cur)
                    image_n = src.count
                    plan.notes.append(f"Filesystem rebuilt for LBA {cur}.")
                else:
                    plan.errors.append(f"Session {k + 1} must start at LBA {t.lba} but would start at {cur}.")
            mode = t.mode
            if final and i == 0 and mode == S.MODE1:
                mode = S.MODE2
                plan.notes.append("Boot track is Mode 1 in the image; writing it as Mode 2 XA "
                                  "(what MIL-CD boot expects).")
            if t.mode == S.MODE2:
                tail = S.trailing_form2(src)
                if tail:
                    src = S.SliceSource(src, 0, src.count - tail)
            pad = 0
            if i < len(sess) - 1:
                target = sess[i + 1].lba - PREGAP - lba
            elif not final:
                target = disc.sessions[k + 1][0].lba - session_gap(k) - RUNOUT - lba
            else:
                target = max(src.count, MIN_TRACK)
                fs_end = iso_sectors(src, lba) if mode != S.AUDIO and i == 0 else None
                if fs_end:
                    room = capacity - (lba + RUNOUT)
                    target = max(target, min(fs_end + FINAL_PAD, room))
                    pad = max(0, target - src.count)
            if target < MIN_TRACK:
                plan.errors.append(f"Session {k + 1} track {i + 1} would be only {target} sectors "
                                   f"(minimum {MIN_TRACK}); this image layout can't be burned.")
                target = MIN_TRACK
            if target < src.count:
                drop = src.count - target
                if drop > RUNOUT and not (t.mode == S.AUDIO and drop <= 75) and not _blank(src, target, drop):
                    plan.errors.append(f"Session {k + 1} track {i + 1}: {drop} sectors of real data would have "
                                       "to be cut to keep the layout; refusing.")
                src = S.SliceSource(src, 0, target)
            elif target > src.count:
                src = S.fit(src, target)
            tracks.append(PlanTrack(mode, lba, src, t.label, image_n, pad))
            lba = lba + target + PREGAP
        ps = PlanSession(k, tracks, final)
        plan.sessions.append(ps)
        cur = ps.end_lba + session_gap(k)

    boot = plan.boot
    if boot.mode == S.AUDIO:
        plan.errors.append("The last session starts with an audio track: there is nothing to boot.")
    elif region_free or vga:
        raw = boot.source.read(0, 16)
        info = ipbin.parse(raw)
        if info.valid:
            patched = ipbin.patch(raw, region_free=region_free, vga=vga)
            if patched != raw:
                boot.source = S.OverlaySource(boot.source, 0, patched)
                done = [n for n, on in (("region-free", region_free), ("VGA", vga)) if on]
                plan.notes.append("IP.BIN patched: " + " + ".join(done) + ".")

    if plan.end_lba > capacity:
        need, have = fmt_mb(plan.end_lba), fmt_mb(capacity)
        plan.errors.append(f"Too big for the disc: needs {need} of space, the CD-R holds {have}.")
    elif plan.end_lba > capacity * NEARLY_FULL:
        plan.notes.append("The disc will be almost full. The outer edge is the hardest part for an old "
                          "Dreamcast to read, so use a good-quality CD-R.")
    return plan
