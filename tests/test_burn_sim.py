"""The real Burner (prepare -> cdrecord -> next-session check -> verify) against a simulated
TAO CD-R drive (tests/fake_drive).  python3 -m unittest discover -s tests

Set DISCFORGE_KEEP_SIM=dir to also export each simulated disc as a CDI (to boot in Flycast).
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["XDG_CACHE_HOME"] = __import__("tempfile").mkdtemp(prefix="discforge-test-cache-")

from discforge import burner as B  # noqa: E402
from discforge import cdi, iso9660  # noqa: E402
from discforge import plan as P  # noqa: E402
from discforge import scramble as SCR  # noqa: E402
from discforge import sectors as S  # noqa: E402
from discforge.loader import load_any  # noqa: E402

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_drive")
ASSETS = os.path.expanduser(os.environ.get("DISCFORGE_TEST_ASSETS", "~/.cache/discforge-dev"))
DS_BOOT = os.path.join(ASSETS, "dl-dreamshell/DreamShell_bootloader_v2.9.cdi")
HAVE_MKISOFS = shutil.which("mkisofs") is not None


class SimDrive:
    def __init__(self, **knobs):
        self.knobs = knobs

    def __enter__(self):
        if sys.platform.startswith("win"):
            raise unittest.SkipTest("the simulated drive is a set of POSIX scripts")
        self.dir = tempfile.mkdtemp(prefix="fake-drive-")
        self.dev = os.path.join(self.dir, "sr0")
        open(self.dev, "w").close()
        self.env = dict(os.environ)
        os.environ["FAKE_DRIVE_DIR"] = self.dir
        os.environ["PATH"] = FAKE + os.pathsep + os.environ["PATH"]
        st = {"tracks": [], "sessions": 0, "closed": False, "nwa": 0, "busy_fail": 0, "extra_gap": 0, "calls": []}
        st.update(self.knobs)
        with open(os.path.join(self.dir, "state.json"), "w") as f:
            json.dump(st, f)
        self.saved = (B.drive_status, B.last_session, B.read_toc, B.block_size, B.eject)
        B.drive_status = lambda dev: B.CDS_DISC_OK
        B.last_session = lambda dev: (self.session_tracks(self.state()["sessions"])[0]["lba"], True)
        B.read_toc = lambda dev: ([(i + 1, t["lba"], t["mode"] != "audio") for i, t in enumerate(self.state()["tracks"])]
                                  + [(0xAA, self.state()["tracks"][-1]["lba"] + self.state()["tracks"][-1]["sectors"] + 2, True)])
        B.block_size = lambda dev: 0          # stale kernel size: forces the readcd verify path
        B.eject = lambda dev: True
        return self

    def __exit__(self, *exc):
        os.environ.clear()
        os.environ.update(self.env)
        B.drive_status, B.last_session, B.read_toc, B.block_size, B.eject = self.saved
        keep = os.environ.get("DISCFORGE_KEEP_SIM")
        if keep and self.state()["tracks"]:
            os.makedirs(keep, exist_ok=True)
            self.export_cdi(os.path.join(keep, f"sim-{os.path.basename(self.dir)}.cdi"))
        shutil.rmtree(self.dir, ignore_errors=True)
        return False

    def state(self) -> dict:
        with open(os.path.join(self.dir, "state.json")) as f:
            return json.load(f)

    def session_tracks(self, n: int) -> list[dict]:
        return [t for t in self.state()["tracks"] if t["session"] == n]

    def writes(self) -> list[list[str]]:
        return [c for c in self.state()["calls"] if not any(x in c for x in ("-minfo", "-prcap", "-msinfo", "-atip", "-toc"))]

    def read(self, lba: int, n: int) -> bytes:
        with open(os.path.join(self.dir, "disc.bin"), "rb") as f:
            f.seek(lba * 2048)
            return f.read(n * 2048)

    def export_cdi(self, path: str) -> None:
        """The simulated finished CD-R as a CDI, laid out exactly as the 'drive' wrote it."""
        sessions = []
        for n in range(1, self.state()["sessions"] + 1):
            ws = []
            for t in self.session_tracks(n):
                if t["mode"] == "audio":
                    ws.append(cdi.WriterTrack(S.AUDIO, t["lba"], S.ZeroSource(t["sectors"], S.RAW_SIZE), runout=2))
                else:
                    src = S.FileSectorSource(os.path.join(self.dir, "disc.bin"), t["lba"] * 2048, t["sectors"], 2048, S.MODE2)
                    ws.append(cdi.WriterTrack(S.MODE2, t["lba"], S.BytesSource(src.read(0, t["sectors"])), runout=2))
            sessions.append(ws)
        cdi.write_cdi(path, sessions, "SIMULATED")


def burn(plan, sim, **opts):
    o = B.BurnOptions(dev=sim.dev, speed=10, **opts)
    return B.Burner(o, log=lambda m: None).burn(plan)


@unittest.skipUnless(os.path.exists(DS_BOOT), "DreamShell test image not present")
class CdiBurnTests(unittest.TestCase):
    def test_full_burn_layout_arguments_and_verify(self):
        plan = P.make_plan(load_any(DS_BOOT), region_free=True, vga=True)
        with SimDrive() as sim:
            res = burn(plan, sim)
            self.assertTrue(res.ok)
            self.assertTrue(res.verified)
            st = sim.state()
            self.assertTrue(st["closed"])
            (a,), (d,) = sim.session_tracks(1), sim.session_tracks(2)
            self.assertEqual((a["mode"], a["lba"], a["sectors"]), ("audio", 0, 300))
            self.assertEqual((d["mode"], d["lba"], d["sectors"]), ("xa", 11702, plan.boot.sectors))
            w1, w2 = sim.writes()
            for w in (w1, w2):
                for flag in ("-v", "-tao", "gracetime=3", "speed=10", "driveropts=burnfree"):
                    self.assertIn(flag, w)
            self.assertIn("-multi", w1)
            self.assertIn("-audio", w1)
            self.assertNotIn("-multi", w2)
            self.assertIn("-xa", w2)
            self.assertEqual(sim.read(11702, plan.boot.sectors), plan.boot.source.read(0, plan.boot.sectors))
            fs = iso9660.IsoFS(lambda lba: sim.read(lba, 1), 11702)
            boot = fs.find("1DS_BOOT.BIN")
            self.assertIsNotNone(boot)
            self.assertIs(SCR.looks_scrambled(fs.read_file(boot)), True)

    def test_drive_that_shifts_the_session_stops_before_session_2(self):
        plan = P.make_plan(load_any(DS_BOOT))
        with SimDrive(extra_gap=5) as sim:
            with self.assertRaises(B.BurnError) as cm:
                burn(plan, sim)
            self.assertIn("11707", str(cm.exception))
            self.assertEqual(len(sim.writes()), 1)          # session 2 was never written

    def test_busy_drive_is_retried(self):
        plan = P.make_plan(load_any(DS_BOOT))
        with SimDrive(busy_fail=1) as sim:
            res = burn(plan, sim)
            self.assertTrue(res.ok and res.verified)
            self.assertEqual(len(sim.writes()), 3)          # 1 busy attempt + 2 sessions

    def test_practice_run_writes_nothing(self):
        plan = P.make_plan(load_any(DS_BOOT))
        with SimDrive() as sim:
            res = burn(plan, sim, simulate=True)
            self.assertTrue(res.ok and res.simulated)
            self.assertEqual(sim.state()["tracks"], [])
            self.assertTrue(all("-dummy" in w for w in sim.writes()))

    def test_plan_object_is_not_changed_by_a_burn(self):
        plan = P.make_plan(load_any(DS_BOOT))
        before = [(t.lba, t.sectors, id(t)) for s in plan.sessions for t in s.tracks]
        with SimDrive() as sim:
            burn(plan, sim)
        self.assertEqual(before, [(t.lba, t.sectors, id(t)) for s in plan.sessions for t in s.tracks])


@unittest.skipUnless(HAVE_MKISOFS, "mkisofs not installed")
class HomebrewBurnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "game", "data"))
        self.prog = os.urandom(50000)
        with open(os.path.join(self.tmp, "game", "game.bin"), "wb") as f:
            f.write(self.prog)
        with open(os.path.join(self.tmp, "game", "data", "level.dat"), "wb") as f:
            f.write(b"level data" * 500)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_homebrew_follows_a_shifted_session(self):
        disc = load_any(os.path.join(self.tmp, "game"), scrambled=False)
        try:
            plan = P.make_plan(disc)
            with SimDrive(extra_gap=5) as sim:
                res = burn(plan, sim)
                self.assertTrue(res.ok and res.verified)
                (d,) = sim.session_tracks(2)
                self.assertEqual(d["lba"], 11707)
                self.assertTrue(iso9660.root_is_consistent(lambda lba: sim.read(lba, 1), 11707))
                fs = iso9660.IsoFS(lambda lba: sim.read(lba, 1), 11707)
                self.assertEqual(fs.read_file(fs.find("1ST_READ.BIN")), SCR.scramble(self.prog))
        finally:
            disc.cleanup()


if __name__ == "__main__":
    unittest.main()
