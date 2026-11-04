"""Multi-console engine: detection, conversions and simulated CD/DVD burns with synthetic images.
python3 -m unittest discover -s tests"""
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from discforge import burner as B  # noqa: E402
from discforge import cdraw  # noqa: E402
from discforge import cue as CUE  # noqa: E402
from discforge import detect as D  # noqa: E402
from discforge import formats as F  # noqa: E402
from discforge import jobs as J  # noqa: E402
from discforge import sectors as S  # noqa: E402
from discforge import tools  # noqa: E402
from discforge import xbox as X  # noqa: E402

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_drive")
HAVE_MKISOFS = shutil.which("mkisofs") is not None
SYNC = b"\x00" + b"\xff" * 10 + b"\x00"


def bcd(n):
    return ((n // 10) << 4) | (n % 10)


def raw_mode2(user: bytes, start_lba: int = 0) -> bytes:
    body = S.encode_mode2_form1(user)
    out = bytearray()
    for i in range(len(user) // 2048):
        a = start_lba + i + 150
        out += SYNC + bytes([bcd(a // 4500), bcd(a // 75 % 60), bcd(a % 75), 2]) + body[i * 2336:(i + 1) * 2336]
    return bytes(out)


def mkiso(folder: str, out: str, *extra: str) -> str:
    if not HAVE_MKISOFS:
        raise unittest.SkipTest("mkisofs not installed")
    subprocess.run(["mkisofs", "-quiet", "-o", out, *extra, folder], check=True)
    return out


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="df-test-")
        self.env = dict(os.environ)
        os.environ["XDG_CACHE_HOME"] = os.path.join(self.tmp, "cache")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def p(self, *parts):
        return os.path.join(self.tmp, *parts)

    def write(self, rel: str, data: bytes) -> str:
        path = self.p(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def ps1_disc(self, audio=True) -> str:
        """A PlayStation CUE/BIN: raw Mode 2 data track (+ an audio track with a 2 s pregap in its bin)."""
        self.write("ps1src/SYSTEM.CNF", b"BOOT = cdrom:\\SLUS_012.34;1\r\nTCB = 4\r\n")
        self.write("ps1src/SLUS_012.34", os.urandom(300_000))
        iso = mkiso(self.p("ps1src"), self.p("ps1.iso"), "-sysid", "PLAYSTATION")
        with open(iso, "rb") as f:
            user = f.read()
        self.write("ps1/Game (Track 1).bin", raw_mode2(user))
        cue = 'FILE "Game (Track 1).bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n'
        if audio:
            self.audio = os.urandom(2352 * 500)
            self.write("ps1/Game (Track 2).bin", self.audio)
            cue += 'FILE "Game (Track 2).bin" BINARY\n  TRACK 02 AUDIO\n    INDEX 00 00:00:00\n    INDEX 01 00:02:00\n'
        return self.write("ps1/Game (USA).cue", cue.encode())


class SimDrive:
    """Fake cdrecord/cdrdao/growisofs on PATH plus patched ioctls."""

    def __init__(self, tmp: str, media="CD-R", **knobs):
        self.dir = os.path.join(tmp, "drive")
        os.makedirs(self.dir, exist_ok=True)
        self.dev = os.path.join(self.dir, "sr0")
        open(self.dev, "wb").close()
        st = {"tracks": [], "sessions": 0, "closed": False, "nwa": 0, "busy_fail": 0, "extra_gap": 0, "calls": [],
              "media": media}
        st.update(knobs)
        with open(os.path.join(self.dir, "state.json"), "w") as f:
            json.dump(st, f)

    def __enter__(self):
        if sys.platform.startswith("win"):
            raise unittest.SkipTest("the simulated drive is a set of POSIX scripts")
        self.saved = (B.drive_status, B.read_toc, B.eject, cdraw.RawCd, B.last_session, B.block_size)
        os.environ["FAKE_DRIVE_DIR"] = self.dir
        os.environ["PATH"] = FAKE + os.pathsep + os.environ["PATH"]
        B.drive_status = lambda dev: B.CDS_DISC_OK
        B.read_toc = self._toc
        B.eject = lambda dev: True
        B.block_size = lambda dev: 0                       # stale size: Dreamcast verify uses readcd
        B.last_session = lambda dev: ([t for t in self.state()["tracks"]
                                       if t["session"] == self.state()["sessions"]][0]["lba"], True)
        sim = self

        class FakeRaw:
            def __init__(self, dev):
                pass

            def read(self, lba, n, user_only=False):
                name, ss = ("disc.user", 2048) if user_only else ("disc.raw", 2352)
                with open(os.path.join(sim.dir, name), "rb") as f:
                    f.seek(lba * ss)
                    return f.read(n * ss)

            def close(self):
                pass

        cdraw.RawCd = FakeRaw
        return self

    def __exit__(self, *exc):
        B.drive_status, B.read_toc, B.eject, cdraw.RawCd, B.last_session, B.block_size = self.saved
        return False

    def _toc(self, dev):
        st = self.state()
        if "cd_toc" in st:                                 # cdrdao (disc at once)
            return [tuple(t) for t in st["cd_toc"]]
        t = st["tracks"]                                   # cdrecord (track at once, Dreamcast)
        return ([(i + 1, x["lba"], x["mode"] != "audio") for i, x in enumerate(t)]
                + [(0xAA, t[-1]["lba"] + t[-1]["sectors"] + 2, True)])

    def state(self):
        with open(os.path.join(self.dir, "state.json")) as f:
            return json.load(f)

    def writes(self, tool):
        return [c for c in self.state()["calls"] if c[0] == tool and c[1] in ("write", "simulate", "-dvd-compat")]

    def dvd_write(self):
        """The one DVD write call: growisofs where it's used, else cdrecord disc-at-once."""
        if tools.DVD_WITH_CDRECORD:
            (w,) = [c for c in self.state()["calls"] if "-sao" in c]
        else:
            (w,) = self.writes("growisofs")
        return w


def burn(job, sim, **kw):
    return J.burn_job(job, B.BurnOptions(dev=sim.dev, speed=None, **kw), log=lambda m: None)


# --------------------------------------------------------------------------

class CueTests(Tmp):
    def test_parse_layout_and_toc(self):
        self.write("c/a.bin", bytes(2352 * 400))
        self.write("c/b.bin", bytes(2352 * 750))
        self.write("c/x.cue", b'FILE "a.bin" BINARY\n TRACK 01 MODE1/2352\n  INDEX 01 00:00:00\n'
                               b'FILE "b.bin" BINARY\n TRACK 02 AUDIO\n  INDEX 00 00:00:00\n  INDEX 01 00:02:00\n'
                               b' TRACK 03 AUDIO\n  PREGAP 00:01:00\n  INDEX 01 00:06:00\n')
        sheet = CUE.parse_cue(self.p("c/x.cue"))
        self.assertEqual([t.length for t in sheet.tracks], [400, 450, 300])
        self.assertEqual(CUE.layout(sheet), [(1, 0, True), (2, 550, False), (3, 925, False)])
        toc = self.p("c/x.toc")
        self.assertTrue(CUE.write_toc(sheet, toc))
        if shutil.which("cdrdao"):
            out = subprocess.run(["cdrdao", "show-toc", toc], capture_output=True, text=True).stdout
            starts = [int(s) for s in __import__("re").findall(r"START\s+\S+\(\s*(\d+)\)", out)]
            self.assertEqual(starts, [0, 550, 925])

    def test_case_and_path_tolerance(self):
        self.write("c/Track 01.BIN", bytes(2352 * 300))
        self.write("c/y.cue", b'FILE "C:\\rips\\track 01.bin" BINARY\n TRACK 01 MODE2/2352\n  INDEX 01 00:00:00\n')
        self.assertEqual(CUE.parse_cue(self.p("c/y.cue")).tracks[0].length, 300)

    def test_ccd(self):
        self.write("cc/g.img", raw_mode2(bytes(2048 * 400)) + bytes(2352 * 300))
        self.write("cc/g.ccd", b"[CloneCD]\nVersion=3\n[Disc]\nTocEntries=4\nSessions=1\n"
                               b"[TRACK 1]\nMODE=2\nINDEX 1=0\n[TRACK 2]\nMODE=0\nINDEX 0=400\nINDEX 1=450\n")
        sheet = D.parse_ccd(self.p("cc/g.ccd"))
        self.assertEqual([(t.mode, t.start, t.length) for t in sheet.tracks],
                         [("MODE2/2352", 0, 400), ("AUDIO", 400, 300)])
        self.assertEqual(CUE.layout(sheet), [(1, 0, True), (2, 450, False)])


@unittest.skipUnless(HAVE_MKISOFS, "mkisofs not installed")
class PlayStationTests(Tmp):
    def test_identify_ps1(self):
        ident = D.identify(self.ps1_disc())
        self.assertEqual((ident.platform, ident.game_id, ident.method), ("ps1", "SLUS-01234", "cd"))
        self.assertEqual(ident.title, "Game")

    def test_ps1_burn_raw_with_audio_and_verify(self):
        job = J.open_job(self.ps1_disc())
        self.assertTrue(job.cd.swap and job.cd.raw)
        with SimDrive(self.tmp) as sim:
            res = burn(job, sim)
            self.assertTrue(res.ok and res.verified, res.messages)
            st = sim.state()
            self.assertEqual(st["driver_used"], "generic-mmc-raw")
            self.assertTrue(st["swap_used"])
            self.assertEqual([t[:2] for t in st["cd_toc"][:2]], [[1, 0], [2, job.cd.layout[1][1]]])
            with open(os.path.join(sim.dir, "disc.raw"), "rb") as f:
                f.seek(job.cd.layout[1][1] * 2352 - 150 * 2352)
                self.assertEqual(f.read(len(self.audio)), self.audio)      # pregap + audio, byte exact
        job.cleanup()

    def test_raw_unsupported_falls_back_to_cooked(self):
        job = J.open_job(self.ps1_disc(audio=False))
        with SimDrive(self.tmp, raw_unsupported=True) as sim:
            res = burn(job, sim)
            self.assertTrue(res.ok and res.verified)
            drivers = [c[c.index("--driver") + 1] for c in sim.writes("cdrdao")]
            self.assertEqual(drivers, ["generic-mmc-raw", "generic-mmc"])

    def test_busy_drive_retried_and_write_error_reported(self):
        job = J.open_job(self.ps1_disc(audio=False))
        with SimDrive(self.tmp, busy_fail=1) as sim:
            self.assertTrue(burn(job, sim).ok)
        with SimDrive(self.tmp + "/2", write_error=True) as sim:
            with self.assertRaises(B.BurnError) as cm:
                burn(job, sim)
            self.assertIn("write error", str(cm.exception))

    def test_dvd_rejected_for_cd_game(self):
        job = J.open_job(self.ps1_disc(audio=False))
        with SimDrive(self.tmp, media="DVD-R") as sim:
            with self.assertRaises(B.BurnError) as cm:
                burn(job, sim)
            self.assertIn("CD-R", str(cm.exception))

    def ps2_iso(self, size=600_000) -> str:
        self.write("ps2src/SYSTEM.CNF", b"BOOT2 = cdrom0:\\SLUS_200.01;1\r\nVER = 1.00\r\nVMODE = NTSC\r\n")
        self.write("ps2src/SLUS_200.01", os.urandom(size))
        return mkiso(self.p("ps2src"), self.p("Some Game (USA).iso"), "-sysid", "PLAYSTATION", "-udf")

    def test_ps2_dvd_burn_dvd_r_and_plus_r(self):
        iso = self.ps2_iso()
        ident = D.identify(iso)
        self.assertEqual((ident.platform, ident.method, ident.media, ident.game_id), ("ps2", "dvd", "dvd", "SLUS-20001"))
        job = J.open_job(iso)
        with SimDrive(self.tmp, media="DVD-R") as sim:
            res = burn(job, sim)
            self.assertTrue(res.ok and res.verified)
            w = sim.dvd_write()
            if tools.DVD_WITH_CDRECORD:              # Windows / macOS: cdrecord, disc-at-once
                self.assertIn("speed=4", w)
            else:
                self.assertIn("-use-the-force-luke=dao", w)
                self.assertIn("-speed=4", w)
            self.assertFalse(any(c[0] == "dvd+rw-booktype" for c in sim.state()["calls"]))
        with SimDrive(self.tmp + "/plus", media="DVD+R") as sim:
            res = burn(job, sim)
            self.assertTrue(res.ok and res.verified)
            w = sim.dvd_write()
            self.assertNotIn("-use-the-force-luke=dao", w)
            if not tools.DVD_WITH_CDRECORD:
                self.assertTrue(any(c[0] == "dvd+rw-booktype" for c in sim.state()["calls"]))

    def test_dual_layer_break_reaches_the_burner(self):
        """Whichever burner the system uses, the game's own layer break must be the one written."""
        from discforge import dvd as DVD
        iso = self.ps2_iso()
        job = J.open_job(iso)
        plan = DVD.DvdPlan(job.dvd.image, job.dvd.sectors, "dvd-dl", 2_000_000, "t", 4, True, True, "ps2")
        m = B.Media(present=True, kind="DVD+R/DL", blank=True, capacity=4_171_712)
        cmd = DVD.CdrecordDvdBurner(B.BurnOptions(dev="x")).command(plan, m, {}, "DVD+R/DL")
        self.assertIn("driveropts=burnfree,layerbreak=2000000", cmd)
        self.assertFalse(any(a.startswith("layerbreak=") for a in cmd))

    def test_ps2_cd_iso_goes_to_cd_as_mode2(self):
        self.write("p2cd/SYSTEM.CNF", b"BOOT2 = cdrom0:\\SLPS_250.01;1\r\n")
        iso = mkiso(self.p("p2cd"), self.p("cd.iso"), "-sysid", "PLAYSTATION")
        job = J.open_job(iso)
        self.assertEqual((job.platform.id, job.method), ("ps2", "cd"))
        with open(job.cd.toc) as f:
            self.assertIn("MODE2_FORM1", f.read())
        with SimDrive(self.tmp) as sim:
            res = burn(job, sim)
            self.assertTrue(res.ok and res.verified)


class NintendoTests(Tmp):
    def gc_image(self) -> str:
        h = bytearray(0x440)
        h[0:6] = b"GTEST1"
        h[0x1C:0x20] = D.GC_MAGIC
        h[0x20:0x30] = b"Test Cube Game\0\0"
        return self.write("gc/game.iso", bytes(h) + os.urandom(3 << 20))

    def test_gamecube_identify_and_burn(self):
        ident = D.identify(self.gc_image())
        self.assertEqual((ident.platform, ident.title, ident.game_id, ident.media),
                         ("gamecube", "Test Cube Game", "GTEST1", "dvd"))
        job = J.open_job(self.gc_image())
        with SimDrive(self.tmp, media="DVD-R") as sim:
            self.assertTrue(burn(job, sim).verified)

    def test_wbfs_to_iso(self):
        blk = 1 << 21
        hdr = bytearray(0x100)
        hdr[0:6] = b"RTEST1"
        hdr[0x18:0x1C] = D.WII_MAGIC
        hdr[0x20:0x2C] = b"Wii Test Fun"
        b0 = bytes(hdr) + os.urandom(blk - 0x100)
        b7 = os.urandom(blk)
        nblocks = F.NUM_WII_SECTORS >> (21 - 15)
        info = bytearray(hdr) + bytearray(2 * nblocks)
        struct.pack_into(">H", info, 0x100, 1)
        struct.pack_into(">H", info, 0x100 + 14, 2)
        head = bytearray(512)
        head[0:4] = b"WBFS"
        struct.pack_into(">I", head, 4, 3 * blk // 512)
        head[8], head[9], head[12] = 9, 21, 1
        wbfs = bytearray(blk)
        wbfs[0:512] = head
        wbfs[512:512 + len(info)] = info
        path = self.write("wii/Wii Test Fun (USA).wbfs", bytes(wbfs) + b0 + b7)
        ident = D.identify(path)
        self.assertEqual((ident.platform, ident.container, ident.title, ident.size),
                         ("wii", "wbfs", "Wii Test Fun", F.WII_SL_SIZE))
        with F.open_reader(path) as r:
            self.assertEqual(r.read(0, blk), b0)
            self.assertEqual(r.read(7 * blk, blk), b7)
            self.assertEqual(r.read(3 * blk, 4096), bytes(4096))
        job = J.open_job(path)
        self.assertEqual((job.platform.id, job.method, job.sectors), ("wii", "dvd", F.WII_SL_SIZE // 2048))
        with open(job.dvd.image, "rb") as f:
            f.seek(7 * blk)
            self.assertEqual(f.read(blk), b7)

    def test_gcz_cso_zso_readers(self):
        data = os.urandom(200_000) + bytes(300_000) + os.urandom(100_000)
        data += bytes(-len(data) % 2048)
        bs = 16384
        blocks = [data[i:i + bs] for i in range(0, len(data), bs)]
        # GCZ
        comp = [zlib.compress(b) for b in blocks]
        ptrs, pos, body = [], 0, b""
        for b, c in zip(blocks, comp):
            if len(c) >= len(b):
                ptrs.append(pos | (1 << 63))
                body += b
                pos += len(b)
            else:
                ptrs.append(pos)
                body += c
                pos += len(c)
        gcz = struct.pack("<IIQQII", 0xB10BC001, 0, len(body), len(data), bs, len(blocks))
        gcz += struct.pack(f"<{len(ptrs)}Q", *ptrs) + bytes(4 * len(ptrs)) + body
        with F.open_reader(self.write("g.gcz", gcz)) as r:
            self.assertEqual(r.read(0, len(data)), data)
            self.assertEqual(r.read(12345, 50000), data[12345:62345])
        # CSO (deflate) and ZSO (lz4)
        import lz4.block
        for magic, pack in ((b"CISO", lambda b: zlib.compressobj(9, zlib.DEFLATED, -15).compress(b) +
                             zlib.compressobj(9, zlib.DEFLATED, -15).flush()),
                            (b"ZISO", lambda b: lz4.block.compress(b, store_size=False))):
            idx, body = [], b""
            base = 24 + 4 * (len(blocks) + 1)
            for b in blocks:
                c = pack(b) if magic == b"ZISO" else zlib.compress(b)[2:-4]
                if len(c) >= len(b):
                    idx.append((base + len(body)) | 0x80000000)
                    body += b
                else:
                    idx.append(base + len(body))
                    body += c
            idx.append(base + len(body))
            cso = magic + struct.pack("<IQIBB2x", 24, len(data), bs, 1, 0) + struct.pack(f"<{len(idx)}I", *idx) + body
            with F.open_reader(self.write("g." + magic.decode().lower(), cso)) as r:
                self.assertEqual(r.read(0, len(data)), data, magic)


class XboxTests(Tmp):
    def xbe(self, title="Test Game") -> bytes:
        x = bytearray(0x1000)
        x[0:4] = b"XBEH"
        struct.pack_into("<I", x, 0x104, 0x10000)
        struct.pack_into("<I", x, 0x118, 0x10200)
        struct.pack_into("<I", x, 0x208, 0x4D530004)
        t = title.encode("utf-16-le")
        x[0x20C:0x20C + len(t)] = t
        return bytes(x)

    def folder(self) -> dict:
        files = {"default.xbe": self.xbe(), "a.txt": b"hello", "Media/x.bin": os.urandom(5000),
                 "media2/Y.BIN": os.urandom(70000), "zero.dat": b""}
        for i in range(140):
            files[f"Levels/level_{i:03d}_with_a_long_name.map"] = os.urandom(100 + i)
        for rel, data in files.items():
            self.write("xb/" + rel, data)
        os.makedirs(self.p("xb", "Empty"))
        return files

    def check(self, image: str, files: dict):
        with F.FileReader(image) as r:
            x = X.XdvdfsReader(r, X.partition_offset(r))
            for rel, data in files.items():
                e = x.find(rel)
                self.assertIsNotNone(e, rel)
                self.assertEqual(x.read_file(e), data, rel)
                parent = x._tree
                for part in rel.split("/")[:-1]:
                    parent = next(c for c in parent.children if c.text.upper() == part.upper())
                table = r.read(X.partition_offset(r) + parent.sector * 2048, parent.size)
                self.assertTrue(self.bst_find(table, rel.split("/")[-1].encode()), rel)
            self.assertEqual(x.find("Empty").children, [])

    @staticmethod
    def bst_find(table: bytes, name: bytes) -> bool:
        off, key = 0, name.upper()
        while True:
            left, right, _s, _z, _a, n = struct.unpack_from("<HHIIBB", table, off)
            here = table[off + 14:off + 14 + n].upper()
            if key == here:
                return True
            nxt = left if key < here else right
            if not nxt:
                return False
            off = nxt * 4

    def test_folder_to_xiso_and_identify(self):
        files = self.folder()
        ident = D.identify(self.p("xb"))
        self.assertEqual((ident.platform, ident.title, ident.game_id, ident.container),
                         ("xbox", "Test Game", "MS-004", "xbox-folder"))
        out = self.p("out.xiso")
        size, patched = X.folder_to_xiso(self.p("xb"), out)
        self.assertEqual(patched, 0)
        self.assertEqual(size, os.path.getsize(out))
        self.assertEqual(size % 0x10000, 0)
        self.check(out, files)

    def test_redump_image_is_trimmed(self):
        files = self.folder()
        xiso = self.p("g.xiso")
        X.folder_to_xiso(self.p("xb"), xiso)
        redump = self.p("Test (USA).iso")
        with open(redump, "wb") as f:
            f.truncate(X.REDUMP_GAME_OFF + (64 << 20))       # a sparse 'video partition', then the game
            f.seek(X.REDUMP_GAME_OFF)
            with open(xiso, "rb") as src:
                f.write(src.read())
        ident = D.identify(redump)
        self.assertEqual((ident.platform, ident.container), ("xbox", "xbox-redump"))
        job = J.open_job(redump)
        self.assertEqual(job.method, "dvd")
        self.assertLess(os.path.getsize(job.dvd.image), 2 << 20)
        self.check(job.dvd.image, files)
        with SimDrive(self.tmp, media="DVD+R") as sim:
            self.assertTrue(burn(job, sim).verified)


@unittest.skipUnless(HAVE_MKISOFS, "mkisofs not installed")
class OtherTests(Tmp):
    def test_data_folder_to_cd(self):
        self.write("stuff/readme.txt", b"hi" * 1000)
        self.write("stuff/docs/manual.pdf", os.urandom(80000))
        job = J.open_job(self.p("stuff"))
        self.assertEqual((job.platform.id, job.method), ("pc", "cd"))
        with SimDrive(self.tmp) as sim:
            res = burn(job, sim)
            self.assertTrue(res.ok and res.verified)
            self.assertEqual(sim.state()["driver_used"], "generic-mmc")
        job.cleanup()

    def test_ecm_cue(self):
        from test_ecm import Encoder
        cue = self.ps1_disc(audio=False)
        b = self.p("ps1", "Game (Track 1).bin")
        enc = Encoder()
        with open(b, "rb") as f:
            enc.mode2_bin(f.read())
        with open(b + ".ecm", "wb") as f:
            f.write(enc.finish())
        os.unlink(b)
        job = J.open_job(b + ".ecm")
        self.assertEqual((job.platform.id, job.method, job.game_id), ("ps1", "cd", "SLUS-01234"))
        del cue

    @unittest.skipUnless(shutil.which("chdman"), "chdman not installed")
    def test_chd_cd(self):
        cue = self.ps1_disc()
        chd = self.p("game.chd")
        subprocess.run(["chdman", "createcd", "-i", cue, "-o", chd], check=True, capture_output=True)
        job = J.open_job(chd)
        self.assertEqual((job.platform.id, job.method), ("ps1", "cd"))
        self.assertEqual(len(job.cd.layout), 2)
        with SimDrive(self.tmp) as sim:
            self.assertTrue(burn(job, sim).verified)


if __name__ == "__main__":
    unittest.main()
