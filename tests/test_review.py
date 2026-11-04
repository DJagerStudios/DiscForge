"""Tests for the burn-path hardening (review pass).  python3 -m unittest discover -s tests"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["XDG_CACHE_HOME"] = __import__("tempfile").mkdtemp(prefix="discforge-test-cache-")


def write_bytes(path, data):
    with open(path, "wb") as f:
        f.write(data)


def write_text(path, text):
    with open(path, "w") as f:
        f.write(text)


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()

from discforge import burner as B  # noqa: E402
from discforge import cdi, ipbin  # noqa: E402
from discforge import plan as P  # noqa: E402
from discforge import scramble as SCR  # noqa: E402
from discforge import sectors as S  # noqa: E402
from discforge.disc import Disc, Track, load_cdi  # noqa: E402
from discforge.loader import load_any  # noqa: E402

ASSETS = os.path.expanduser(os.environ.get("DISCFORGE_TEST_ASSETS", "~/.cache/discforge-dev"))
DS_BOOT = os.path.join(ASSETS, "dl-dreamshell/DreamShell_bootloader_v2.9.cdi")
HAVE_MKISOFS = shutil.which("mkisofs") is not None


def mkiso(folder: str, out: str, lba: int, ip: bytes | None) -> str:
    cmd = ["mkisofs", "-quiet", "-C", f"0,{lba}", "-l", "-J", "-r", "-o", out]
    if ip is not None:
        ip_path = out + ".ip"
        write_bytes(ip_path, ip)
        cmd[5:5] = ["-G", ip_path]
    subprocess.run(cmd + [folder], check=True, capture_output=True)
    return out


def cdi_from_iso(iso: str, out: str, lba: int = 11702) -> str:
    n = os.path.getsize(iso) // 2048
    cdi.write_cdi(out, [[cdi.WriterTrack(S.AUDIO, 0, S.ZeroSource(lba - 11400, S.RAW_SIZE))],
                        [cdi.WriterTrack(S.MODE2, lba, S.FileSectorSource(iso, 0, n, 2048, S.MODE2), runout=2)]])
    return out


class HumanizeTests(unittest.TestCase):
    NONROOT = ("cdrecord: Operation not permitted. WARNING: Cannot do mlockall(2).\n"
               "cdrecord: Permission denied. WARNING: Cannot set priority using setpriority().\n"
               "cdrecord: WARNING: This causes a high risk for buffer underruns.\n")

    def test_write_error_not_blamed_on_permissions(self):
        reason, _ = B.humanize_failure(self.NONROOT + "write_g1: Input/output error. Sense Key: 0x3 Medium Error")
        self.assertIn("write error", reason)

    def test_plain_failure_with_warnings_is_generic(self):
        reason, _ = B.humanize_failure(self.NONROOT + "cdrecord failed (exit 255)")
        self.assertIn("stopped unexpectedly", reason)

    def test_real_permission_problem(self):
        reason, _ = B.humanize_failure("cdrecord: Permission denied. Cannot open or use SCSI driver.")
        self.assertIn("isn't allowed", reason)

    def test_power_calibration(self):
        self.assertIn("laser", B.humanize_failure("Power calibration area error")[0])


class ParseTests(unittest.TestCase):
    def test_appendable_disc_is_not_blank(self):
        m = B.parse_minfo("Mounted media type:       CD-R\ndisk status:              incomplete/appendable\n")
        self.assertTrue(m.present)
        self.assertFalse(m.blank)
        self.assertEqual(m.status, "incomplete")

    def test_prcap_speeds_and_choice(self):
        out = ("  Does write CD-R media\n"
               "  Write speed # 0:  8467 kB/s CLV/PCAV (CD  48x, DVD  6x, BD  1x)\n"
               "  Write speed # 1:  4234 kB/s CLV/PCAV (CD  24x, DVD  3x, BD  0x)\n"
               "  Write speed # 2:  1764 kB/s CLV/PCAV (CD  10x, DVD  1x, BD  0x)\n")
        caps = B.parse_prcap(out)
        self.assertEqual((caps.writes_cd_r, caps.cd_write_speeds), (True, [10, 24, 48]))
        self.assertEqual(B.choose_speed(caps.cd_write_speeds), 10)
        self.assertEqual(B.choose_speed([4, 8, 16]), 8)
        self.assertEqual(B.choose_speed([4, 6]), 6)
        self.assertEqual(B.choose_speed([]), 8)


class BlockerTests(unittest.TestCase):
    def test_no_ipbin_blocks(self):
        d = Disc("x", "cdi", [[Track(S.AUDIO, 0, S.ZeroSource(302, 2352))],
                              [Track(S.MODE2, 11702, S.ZeroSource(400))]])
        d.analyse()
        self.assertTrue(d.blockers)
        self.assertFalse(P.make_plan(d).ok)

    @unittest.skipUnless(HAVE_MKISOFS, "mkisofs not installed")
    def test_missing_boot_file_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "f"))
            write_bytes(os.path.join(tmp, "f", "OTHER.BIN"), b"x" * 5000)
            iso = mkiso(os.path.join(tmp, "f"), os.path.join(tmp, "a.iso"), 11702, ipbin.build("T"))
            d = load_any(cdi_from_iso(iso, os.path.join(tmp, "a.cdi")))
            self.assertTrue(any("start file" in b for b in d.blockers), d.blockers)


@unittest.skipUnless(HAVE_MKISOFS, "mkisofs not installed")
class PaddingTests(unittest.TestCase):
    def test_padding_after_filesystem_is_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "f"))
            write_bytes(os.path.join(tmp, "f", "1ST_READ.BIN"), SCR.scramble(os.urandom(9000)))
            iso = mkiso(os.path.join(tmp, "f"), os.path.join(tmp, "a.iso"), 11702, ipbin.build("T"))
            fs_size = os.path.getsize(iso) // 2048
            p1 = P.make_plan(load_any(cdi_from_iso(iso, os.path.join(tmp, "a.cdi"))))
            self.assertTrue(p1.ok, p1.errors)
            self.assertEqual(p1.boot.sectors, max(fs_size + P.FINAL_PAD, P.MIN_TRACK))
            out = os.path.join(tmp, "b.cdi")
            cdi.write_cdi(out, p1.cdi_sessions())
            p2 = P.make_plan(load_any(out))
            self.assertEqual([(t.lba, t.sectors) for s in p1.sessions for t in s.tracks],
                             [(t.lba, t.sectors) for s in p2.sessions for t in s.tracks])

    def test_padding_respects_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "f"))
            write_bytes(os.path.join(tmp, "f", "1ST_READ.BIN"), SCR.scramble(os.urandom(9000)))
            iso = mkiso(os.path.join(tmp, "f"), os.path.join(tmp, "a.iso"), 11702, ipbin.build("T"))
            d = load_any(cdi_from_iso(iso, os.path.join(tmp, "a.cdi")))
            n = os.path.getsize(iso) // 2048
            cap = 11702 + max(n, P.MIN_TRACK) + 2 + 20       # only 20 sectors to spare
            p = P.make_plan(d, capacity=cap)
            self.assertTrue(p.ok, p.errors)
            self.assertLessEqual(p.end_lba, cap)


@unittest.skipUnless(HAVE_MKISOFS and os.path.exists(DS_BOOT), "needs mkisofs and the DreamShell test image")
class AutoFixTests(unittest.TestCase):
    def test_unscrambled_cdi_gets_scrambled_in_place(self):
        boot = load_cdi(DS_BOOT)
        e, _ = boot.boot_file()
        plain = SCR.descramble(boot.fs().read_file(e))
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "f"))
            write_bytes(os.path.join(tmp, "f", "1ST_READ.BIN"), plain)
            iso = mkiso(os.path.join(tmp, "f"), os.path.join(tmp, "a.iso"), 11702, ipbin.build("T"))
            d = load_any(cdi_from_iso(iso, os.path.join(tmp, "a.cdi")))
            self.assertTrue(any("scrambled for you" in n for n in d.notes), d.notes + d.warnings)
            entry, _ = d.boot_file()
            self.assertEqual(d.fs().read_file(entry), SCR.scramble(plain))
            p = P.make_plan(d)
            self.assertTrue(p.ok, p.errors)
            fs_bytes = p.boot.source.read(entry.extent - p.boot.lba, (entry.size + 2047) // 2048)[:entry.size]
            self.assertEqual(fs_bytes, SCR.scramble(plain))

    def test_scrambled_cdi_is_left_alone(self):
        d = load_any(DS_BOOT)
        self.assertFalse(any("scrambled for you" in n for n in d.notes))


class BurnerTests(unittest.TestCase):
    def _plan(self, data_source):
        d = Disc("x", "cdi", [[Track(S.AUDIO, 0, S.ZeroSource(302, 2352))], [Track(S.MODE2, 11702, data_source)]])
        return P.make_plan(d)

    def test_private_copy(self):
        p = self._plan(S.ZeroSource(400))
        q = B._private_copy(p)
        q.sessions[1].tracks[0] = None
        self.assertIsNotNone(p.sessions[1].tracks[0])

    def test_form2_sector_aborts_before_anything_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = bytearray(S.encode_mode2_form1(bytes(2048 * 500)))
            raw[2336 * 200 + 2] = 0x20               # sector 200's subheader says Form 2
            path = os.path.join(tmp, "t.bin")
            write_bytes(path, raw)
            p = self._plan(S.FileSectorSource(path, 0, 500, 2336, S.MODE2))
            br = B.Burner(B.BurnOptions(dev="/dev/null"), log=lambda m: None)
            with self.assertRaises(B.BurnError) as cm:
                br._prepare(p.sessions[1], tmp, 0, p.total_write_sectors)
            self.assertIn("Form 2", str(cm.exception))

    def test_transient_failure_is_retried_only_before_writing(self):
        p = self._plan(S.ZeroSource(400))
        br = B.Burner(B.BurnOptions(dev="/dev/null"), log=lambda m: None)
        calls = []

        def fake(s, files, d, t):
            calls.append(1)
            if len(calls) == 1:
                return False, 255, ["cdrecord: Device or resource busy. Cannot open SCSI driver."]
            return True, 0, []

        br._run_cdrecord = fake
        orig_sleep = B.time.sleep
        B.time.sleep = lambda s: None
        try:
            br._record(p.sessions[0], [], 0, 1)
            self.assertEqual(len(calls), 2)
            br._run_cdrecord = lambda s, f, d, t: (True, 255, ["write_g1: Input/output error"])
            with self.assertRaises(B.BurnError):
                br._record(p.sessions[0], [], 0, 1)
        finally:
            B.time.sleep = orig_sleep

    def test_verify_falls_back_to_readcd(self):
        p = self._plan(S.ZeroSource(400))
        br = B.Burner(B.BurnOptions(dev="/dev/null"), log=lambda m: None)
        saved = (B.drive_status, B.last_session, B.read_toc, B.block_size)
        B.drive_status = lambda dev: B.CDS_DISC_OK
        B.last_session = lambda dev: (11702, True)
        B.read_toc = lambda dev: [(1, 0, False), (2, 11702, True), (0xAA, 12300, True)]
        B.block_size = lambda dev: 0                  # kernel still thinks the disc is blank
        used = []
        br._hash_readcd = lambda dev, lba, n: used.append((lba, n)) or "abc"
        br._hash_blockdev = lambda *a: self.fail("block device must not be used with a stale size")
        try:
            self.assertTrue(br.verify(p, "abc"))
            self.assertEqual(used[0][0], 11702)
            self.assertFalse(br.verify(p, "different"))
        finally:
            B.drive_status, B.last_session, B.read_toc, B.block_size = saved


@unittest.skipUnless(HAVE_MKISOFS, "mkisofs not installed")
class IsoTests(unittest.TestCase):
    def test_iso_built_for_11702_without_ipbin(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "f", "DATA"))
            write_bytes(os.path.join(tmp, "f", "1ST_READ.BIN"), SCR.scramble(os.urandom(20000)))
            write_bytes(os.path.join(tmp, "f", "DATA", "LEVEL.DAT"), b"level" * 999)
            iso = mkiso(os.path.join(tmp, "f"), os.path.join(tmp, "x.iso"), 11702, None)
            d = load_any(iso, scrambled=True)
            try:
                self.assertEqual(d.kind, "homebrew")
                self.assertIsNotNone(d.fs().find("DATA"))
                self.assertTrue(P.make_plan(d).ok)
            finally:
                d.cleanup()


if __name__ == "__main__":
    unittest.main()
