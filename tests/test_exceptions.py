"""Images that need special handling to come out exactly right (DiscForge 1.1)."""
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from discforge import cue as CUE, jobs as J, platforms as P, tools    # noqa: E402
from discforge.burner import Media                                    # noqa: E402
from discforge.engine import media_ok                                 # noqa: E402
from test_multi import SimDrive, Tmp, burn, mkiso, raw_mode2         # noqa: E402


def strip16(raw: bytes) -> bytes:
    return b"".join(raw[i + 16:i + 2352] for i in range(0, len(raw), 2352))


class Mode2_2336(Tmp):
    def ps1_2336(self):
        self.write("src/SYSTEM.CNF", b"BOOT = cdrom:\\SLUS_012.34;1\r\nTCB = 4\r\n")
        self.write("src/SLUS_012.34", os.urandom(200_000))
        with open(mkiso(self.p("src"), self.p("x.iso"), "-sysid", "PLAYSTATION"), "rb") as f:
            self.raw = raw_mode2(f.read())
        self.write("g/Game.bin", strip16(self.raw))
        return self.write("g/Game.cue", b'FILE "Game.bin" BINARY\n  TRACK 01 MODE2/2336\n    INDEX 01 00:00:00\n')

    def test_sectors_rebuilt_exactly(self):
        sheet = CUE.parse_cue(self.ps1_2336())
        os.makedirs(self.p("work"))
        out = CUE.expand_2336(sheet, self.p("work"))
        t = out.tracks[0]
        self.assertEqual((t.mode, t.sector_size), ("MODE2/2352", 2352))
        with open(t.file, "rb") as f:
            self.assertEqual(f.read(), self.raw)
        self.assertEqual(sheet.tracks[0].mode, "MODE2/2336")          # the original sheet is untouched

    def test_addresses_follow_the_disc_layout(self):
        self.write("m/a.bin", strip16(raw_mode2(os.urandom(2048 * 300))))
        self.write("m/b.bin", os.urandom(2352 * 400))
        self.write("m/c.bin", strip16(raw_mode2(os.urandom(2048 * 50))))
        cue = self.write("m/m.cue", b'FILE "a.bin" BINARY\n  TRACK 01 MODE2/2336\n    INDEX 01 00:00:00\n'
                                    b'FILE "b.bin" BINARY\n  TRACK 02 AUDIO\n    INDEX 00 00:00:00\n'
                                    b'    INDEX 01 00:02:00\n'
                                    b'FILE "c.bin" BINARY\n  TRACK 03 MODE2/2336\n    INDEX 01 00:00:00\n')
        sheet = CUE.parse_cue(cue)
        os.makedirs(self.p("w"))
        out = CUE.expand_2336(sheet, self.p("w"))
        steps = CUE.plan(out)[0]
        self.assertEqual(steps[2].lba, 300 + 400)
        with open(out.tracks[2].file, "rb") as f:
            head = f.read(16)
        a = 700 + 150
        bcd = lambda n: (n // 10) * 16 + n % 10      # noqa: E731
        self.assertEqual(head[12:16], bytes([bcd(a // 4500), bcd(a // 75 % 60), bcd(a % 75), 2]))

    def test_burns_raw_and_verifies(self):
        job = J.open_job(self.ps1_2336())
        try:
            self.assertEqual((job.platform.id, job.method), ("ps1", "cd"))
            self.assertEqual(job.blockers, [])
            self.assertTrue(job.cd.raw)
            self.assertIn(("Sectors", "2336-byte sectors rebuilt as full raw sectors"), job.details)
            with SimDrive(self.tmp) as sim:
                res = burn(job, sim)
                self.assertTrue(res.ok and res.verified)
                with open(os.path.join(sim.dir, "disc.raw"), "rb") as f:
                    self.assertEqual(f.read(len(self.raw)), self.raw)
        finally:
            job.cleanup()

    def test_shared_file_is_refused(self):
        self.write("s/s.bin", strip16(raw_mode2(os.urandom(2048 * 400))))
        cue = self.write("s/s.cue", b'FILE "s.bin" BINARY\n  TRACK 01 MODE2/2336\n    INDEX 01 00:00:00\n'
                                    b'  TRACK 02 MODE2/2336\n    INDEX 01 00:03:00\n')
        os.makedirs(self.p("w2"))
        with self.assertRaises(CUE.CueError):
            CUE.expand_2336(CUE.parse_cue(cue), self.p("w2"))


@unittest.skipUnless(shutil.which("chdman"), "chdman not installed")
class ChdVariants(Tmp):
    def ps2_iso(self) -> str:
        self.write("p/SYSTEM.CNF", b"BOOT2 = cdrom0:\\SLUS_200.01;1\r\nVER = 1.00\r\nVMODE = NTSC\r\n")
        self.write("p/SLUS_200.01", os.urandom(300_000))
        return mkiso(self.p("p"), self.p("ps2.iso"), "-sysid", "PLAYSTATION")

    def test_dvd_packed_as_hard_disk(self):
        iso = self.ps2_iso()
        chd = self.p("old.chd")
        subprocess.run(["chdman", "createhd", "-i", iso, "-o", chd, "-f"], check=True, capture_output=True)
        job = J.open_job(chd)
        try:
            self.assertEqual((job.platform.id, job.blockers, job.game_id), ("ps2", [], "SLUS-20001"))
        finally:
            job.cleanup()

    def test_dvd_chd(self):
        iso = self.ps2_iso()
        chd = self.p("new.chd")
        subprocess.run(["chdman", "createdvd", "-i", iso, "-o", chd, "-f"], check=True, capture_output=True)
        job = J.open_job(chd)
        try:
            self.assertEqual((job.platform.id, job.blockers), ("ps2", []))
        finally:
            job.cleanup()


class BadInputs(Tmp):
    """Damaged or empty files get a plain message, never a crash."""

    def test_empty_file(self):
        with self.assertRaisesRegex(J.JobError, "empty"):
            J.open_job(self.write("e/game.bin", b""))

    def test_empty_folder(self):
        os.makedirs(self.p("nothing"))
        with self.assertRaisesRegex(J.JobError, "folder is empty"):
            J.open_job(self.p("nothing"))

    def test_damaged_zip(self):
        from discforge.util import DcError
        with self.assertRaisesRegex(DcError, "zip file is damaged"):
            J.open_job(self.write("z/game.zip", os.urandom(5000)))

    def test_truncated_ecm(self):
        from discforge.util import DcError
        with self.assertRaisesRegex(DcError, "ends early"):
            J.open_job(self.write("t/game.bin.ecm", b"ECM\0\x01"))


class MediaChecks(unittest.TestCase):
    def test_cd_rw_refused_for_old_consoles(self):
        rw = Media(present=True, kind="CD-RW", blank=True, erasable=True, capacity=359_844)
        for plat in ("dreamcast", "ps1", "saturn", "segacd", "3do", "pce", "neogeocd", "cd32"):
            self.assertIn("can't read CD-RW", media_ok(rw, "cd", 1000, plat) or "", plat)
        self.assertIsNone(media_ok(rw, "cd", 1000, "pc"))

    def test_layer_break_needs_dvd_plus_r_dl(self):
        dl = Media(present=True, kind="DVD-R DL", blank=True, capacity=4_171_712)
        self.assertIn("DVD+R DL", media_ok(dl, "dvd-dl", 3_000_000, "ps2", plus_dl_only=True))
        self.assertIsNone(media_ok(dl, "dvd-dl", 3_000_000, "xbox"))

    def test_required_tools_per_system(self):
        self.assertIn("cdrdao", tools.REQUIRED)
        self.assertEqual("growisofs" in tools.REQUIRED, not tools.DVD_WITH_CDRECORD)
        self.assertTrue(set(tools.REQUIRED).isdisjoint(tools.OPTIONAL))
        self.assertTrue(P.NO_CDRW <= set(P.PLATFORMS))


if __name__ == "__main__":
    unittest.main()
