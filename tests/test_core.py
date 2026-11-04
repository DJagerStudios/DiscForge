"""Unit tests for discforge.  Run:  python3 -m unittest discover -s tests

Tests that need real homebrew images look in $DISCFORGE_TEST_ASSETS
(default ~/.cache/discforge-dev) and are skipped when the files aren't there.
"""
import os
import random
import shutil
import struct
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
from discforge import cdi, elf, ipbin, iso9660  # noqa: E402
from discforge import plan as P  # noqa: E402
from discforge import scramble as SCR  # noqa: E402
from discforge import sectors as S  # noqa: E402
from discforge.disc import Disc, Track  # noqa: E402

ASSETS = os.path.expanduser(os.environ.get("DISCFORGE_TEST_ASSETS", "~/.cache/discforge-dev"))
DS_BOOT = os.path.join(ASSETS, "dl-dreamshell/DreamShell_bootloader_v2.9.cdi")
OPENMENU = os.path.join(ASSETS, "dl-openmenu/x/1ST_READ.BIN")
HAVE_MKISOFS = shutil.which("mkisofs") is not None


def disc_from(sessions):
    return Disc("test", "cdi", sessions)


class ScrambleTests(unittest.TestCase):
    def test_roundtrip_many_sizes(self):
        rnd = random.Random(1)
        for size in (0, 1, 31, 32, 33, 4095, 65536 + 7, 2 * 1024 * 1024, 2 * 1024 * 1024 + 4097):
            data = bytes(rnd.getrandbits(8) for _ in range(min(size, 4096))) * (size // 4096 + 1)
            data = data[:size]
            out = SCR.scramble(data)
            self.assertEqual(len(out), size)
            self.assertEqual(SCR.descramble(out), data, size)
            if size >= 64:
                self.assertNotEqual(out, data)

    def test_tail_under_32_bytes_is_kept(self):
        data = bytes(range(40))
        self.assertEqual(SCR.scramble(data)[32:], data[32:])

    @unittest.skipUnless(os.path.exists(DS_BOOT), "DreamShell test image not present")
    def test_detects_real_scrambled_binary(self):
        from discforge.disc import load_cdi
        d = load_cdi(DS_BOOT)
        e, _ = d.boot_file()
        data = d.fs().read_file(e)
        self.assertIs(SCR.looks_scrambled(data), True)
        self.assertIs(SCR.looks_scrambled(SCR.descramble(data)), False)

    @unittest.skipUnless(os.path.exists(OPENMENU), "openMenu test binary not present")
    def test_detects_packed_plain_binary(self):
        with open(OPENMENU, "rb") as f:
            data = f.read()
        self.assertIs(SCR.looks_scrambled(data), False)
        self.assertIs(SCR.looks_scrambled(SCR.scramble(data)), True)


class EccTests(unittest.TestCase):
    def test_form2_gap_edc(self):
        self.assertEqual(S.mode2_form2_gap_sector()[0x91C:0x920], bytes.fromhex("3f13b0be"))

    def test_form1_encoding_shape_and_determinism(self):
        user = bytes(range(256)) * 8 * 3
        enc = S.encode_mode2_form1(user)
        self.assertEqual(len(enc), 3 * 2336)
        for i in range(3):
            sec = enc[i * 2336:(i + 1) * 2336]
            self.assertEqual(sec[:8], S.DATA_SUBHEADER)
            self.assertEqual(sec[8:2056], user[i * 2048:(i + 1) * 2048])
        self.assertEqual(S.encode_mode2_form1(user[:2048]), enc[:2336])

    @unittest.skipUnless(os.path.exists(DS_BOOT), "DreamShell test image not present")
    def test_matches_real_image_sectors(self):
        img = cdi.read_cdi(DS_BOOT)
        t = img.sessions[-1][0]
        with open(DS_BOOT, "rb") as f:
            f.seek(t.data_offset)
            raw = f.read(64 * 2336)
        for i in range(64):
            sec = raw[i * 2336:(i + 1) * 2336]
            self.assertEqual(S.encode_mode2_form1(sec[8:2056], sec[:8]), sec, f"sector {i}")


class SourceTests(unittest.TestCase):
    def test_fit_pad_trim_overlay(self):
        src = S.BytesSource(bytes([1]) * 2048 * 4)
        self.assertEqual(S.fit(src, 6).count, 6)
        self.assertEqual(S.fit(src, 6).read(4, 2), bytes(4096))
        self.assertEqual(S.fit(src, 2).count, 2)
        ov = S.OverlaySource(src, 1, bytes([9]) * 2048)
        self.assertEqual(ov.read(0, 3), bytes([1]) * 2048 + bytes([9]) * 2048 + bytes([1]) * 2048)

    def test_concat_rejects_mixed_sector_sizes(self):
        with self.assertRaises(ValueError):
            S.ConcatSource([S.ZeroSource(1, 2352), S.ZeroSource(1, 2048)])


class CdiTests(unittest.TestCase):
    def test_write_read_roundtrip(self):
        rnd = random.Random(2)
        data = bytes(rnd.getrandbits(8) for _ in range(400 * 2048))
        audio = bytes(rnd.getrandbits(8) for _ in range(300 * 2352))
        sessions = [[cdi.WriterTrack(S.AUDIO, 0, S.BytesSource(audio, 2352), runout=2)],
                    [cdi.WriterTrack(S.MODE2, 11702, S.BytesSource(data), runout=2)]]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.cdi")
            cdi.write_cdi(path, sessions, "TEST")
            img = cdi.read_cdi(path)
            self.assertEqual(img.version, cdi.CDI_V35)
            (a,), (d,) = img.sessions
            self.assertEqual((a.mode, a.lba, a.length, a.sector_size), (S.AUDIO, 0, 302, 2352))
            self.assertEqual((d.mode, d.lba, d.length, d.sector_size), (S.MODE2, 11702, 402, 2336))
            self.assertEqual(a.source(path).read(0, 300), audio)
            dsrc = d.source(path)
            self.assertEqual(dsrc.read(0, 400), data)
            self.assertEqual(S.trailing_form2(dsrc), 2)

    def test_rejects_garbage(self):
        with tempfile.NamedTemporaryFile() as f:
            f.write(os.urandom(4096))
            f.flush()
            with self.assertRaises(cdi.CdiError):
                cdi.read_cdi(f.name)


class PlanTests(unittest.TestCase):
    def data(self, n=400):
        src = S.BytesSource(bytes(n * 2048))
        return src

    def test_classic_audio_data_302(self):
        d = disc_from([[Track(S.AUDIO, 0, S.ZeroSource(302, 2352))], [Track(S.MODE2, 11702, self.data())]])
        p = P.make_plan(d)
        self.assertTrue(p.ok, p.errors)
        self.assertEqual(p.sessions[0].tracks[0].sectors, 300)       # run-out blocks dropped
        self.assertEqual(p.sessions[0].end_lba + P.session_gap(0), 11702)
        self.assertEqual(p.boot.lba, 11702)

    def test_image_without_runout_is_padded_not_shifted(self):
        d = disc_from([[Track(S.AUDIO, 0, S.ZeroSource(300, 2352))], [Track(S.MODE2, 11702, self.data())]])
        p = P.make_plan(d)
        self.assertTrue(p.ok)
        self.assertEqual(p.sessions[0].tracks[0].sectors, 300)

    def test_long_first_session_pads_to_target(self):
        d = disc_from([[Track(S.AUDIO, 0, S.ZeroSource(302, 2352))], [Track(S.MODE2, 45000, self.data())]])
        p = P.make_plan(d)
        self.assertTrue(p.ok)
        self.assertEqual(p.sessions[0].tracks[0].sectors, 45000 - 11402)

    def test_refuses_to_cut_real_data(self):
        d = disc_from([[Track(S.MODE1, 0, S.BytesSource(b"\x01" * 2048 * 1000))],
                       [Track(S.MODE2, 11402 + 500, self.data())]])
        p = P.make_plan(d)
        self.assertFalse(p.ok)

    def test_capacity(self):
        big = S.ZeroSource(360000, 2048)
        d = disc_from([[Track(S.AUDIO, 0, S.ZeroSource(302, 2352))], [Track(S.MODE2, 11702, big)]])
        self.assertTrue(any("Too big" in e for e in P.make_plan(d).errors))

    def test_patch_overlay(self):
        ip = ipbin.build("X", regions="U", vga=False)
        src = S.ConcatSource([S.BytesSource(ip), S.ZeroSource(300)])
        d = disc_from([[Track(S.AUDIO, 0, S.ZeroSource(302, 2352))], [Track(S.MODE2, 11702, src)]])
        p = P.make_plan(d, region_free=True, vga=True)
        info = ipbin.parse(p.boot.source.read(0, 16))
        self.assertEqual((info.regions, info.vga), ("JUE", True))
        self.assertEqual(ipbin.parse(d.boot_track.source.read(0, 16)).regions, "U")  # image untouched

    def test_cdi_export_reimports_to_same_plan(self):
        d = disc_from([[Track(S.AUDIO, 0, S.ZeroSource(302, 2352))], [Track(S.MODE2, 11702, self.data(350))]])
        p = P.make_plan(d)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.cdi")
            cdi.write_cdi(path, p.cdi_sessions())
            from discforge.disc import load_cdi
            p2 = P.make_plan(load_cdi(path))

            def layout(pl):
                return [(t.mode, t.lba, t.sectors, t.source.read(0, t.sectors)) for s in pl.sessions for t in s.tracks]
            self.assertEqual(layout(p), layout(p2))


class IpBinTests(unittest.TestCase):
    def test_crc_check_value(self):
        self.assertEqual(ipbin.crc16(b"123456789"), 0x29B1)

    def test_build_and_parse(self):
        raw = ipbin.build("MY GAME", regions="JE", vga=False, product_no="T1234", version="V1.002",
                          release_date="20261104")
        self.assertEqual(len(raw), 0x8000)
        i = ipbin.parse(raw)
        self.assertTrue(i.valid and i.crc_ok)
        self.assertEqual((i.title, i.regions, i.vga, i.wince, i.boot_filename), ("MY GAME", "JE", False, False, "1ST_READ.BIN"))
        self.assertEqual(i.area_symbols, "J E")
        self.assertEqual(raw[0x100:], ipbin.template()[0x100:])   # bootstrap code untouched

    def test_patch(self):
        i = ipbin.parse(ipbin.patch(ipbin.build("X", regions="U", vga=False), region_free=True, vga=True))
        self.assertEqual((i.regions, i.vga), ("JUE", True))
        self.assertTrue(all(i.region_text_ok(r) for r in "JUE"))


class ElfTests(unittest.TestCase):
    def make_elf(self, payload, base=0x8C010000):
        off = 0x100
        e = bytearray(off + len(payload))
        e[0:16] = b"\x7fELF\x01\x01\x01" + bytes(9)
        struct.pack_into("<HHIIIIIHHHHHH", e, 16, 2, 42, 1, base, 52, 0, 0, 52, 32, 1, 40, 0, 0)
        struct.pack_into("<IIIIIIII", e, 52, 1, off, base, base, len(payload), len(payload), 7, 4)
        e[off:] = payload
        return bytes(e)

    def test_segments(self):
        b, base, entry = elf.elf_to_bin(self.make_elf(b"\x09\x00" * 100))
        self.assertEqual((b, base, entry), (b"\x09\x00" * 100, 0x8C010000, 0x8C010000))
        self.assertEqual(elf.check_load_address(base, entry), [])

    def test_wrong_address_warns(self):
        _b, base, entry = elf.elf_to_bin(self.make_elf(b"\x00" * 64, base=0x8C020000))
        self.assertTrue(elf.check_load_address(base, entry))

    def test_wrong_cpu(self):
        e = bytearray(self.make_elf(b"\x00" * 4))
        e[18] = 62  # x86-64
        with self.assertRaises(elf.ElfError):
            elf.elf_to_bin(bytes(e))


@unittest.skipUnless(HAVE_MKISOFS, "mkisofs not installed")
class IsoAndHomebrewTests(unittest.TestCase):
    def test_infer_base_and_homebrew_build(self):
        from discforge.homebrew import homebrew_disc
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "game")
            os.makedirs(os.path.join(folder, "data"))
            prog = os.urandom(70000)
            write_bytes(os.path.join(folder, "program.elf"), ElfTests().make_elf(prog))
            write_bytes(os.path.join(folder, "data", "Level1.dat"), b"hello" * 1000)
            d = homebrew_disc(folder, title="UNIT TEST")
            try:
                bt = d.boot_track
                self.assertEqual(bt.lba, 11702)
                self.assertEqual(iso9660.infer_base(lambda i: bt.source.read(i, 1), bt.sectors), 11702)
                fs = d.fs()
                boot = fs.find("1ST_READ.BIN")
                self.assertEqual(fs.read_file(boot), SCR.scramble(prog))
                self.assertIsNone(fs.find("PROGRAM.ELF"))
                self.assertIsNotNone(fs.find("DATA"))
                self.assertEqual(d.ip().title, "UNIT TEST")
                p = P.make_plan(d)
                self.assertTrue(p.ok, p.errors)
            finally:
                d.cleanup()


class GdiParseTests(unittest.TestCase):
    def test_quoted_names(self):
        from discforge.gdi import parse_gdi
        with tempfile.TemporaryDirectory() as tmp:
            for n in ("track01.bin", "Track 03.bin"):
                write_bytes(os.path.join(tmp, n), bytes(2352 * 10))
            write_bytes(os.path.join(tmp, "track02.raw"), bytes(2352 * 10))
            g = os.path.join(tmp, "g.gdi")
            write_text(g, '3\n1 0 4 2352 track01.bin 0\n2 450 0 2352 track02.raw 0\n3 45000 4 2352 "Track 03.bin" 0\n')
            t = parse_gdi(g)
            self.assertEqual([(x.number, x.lba, x.is_data, x.sectors) for x in t],
                             [(1, 0, True, 10), (2, 450, False, 10), (3, 45000, True, 10)])


class BurnerParseTests(unittest.TestCase):
    MINFO = """Mounted media class:      CD
Mounted media type:       CD-R
Disk Is not erasable
data type:                standard
disk status:              empty
session status:           empty
Supported modes: TAO PACKET SAO SAO/R96P SAO/R96R RAW/R16 RAW/R96P RAW/R96R
Next writable address:              0
Remaining writable size:            359846
"""

    def test_blank_cdr(self):
        m = B.parse_minfo(self.MINFO)
        self.assertTrue(m.present and m.blank and m.is_cd and not m.erasable)
        self.assertEqual((m.capacity, m.nwa), (359846, 0))
        self.assertIn("TAO", m.modes.split())

    def test_no_disc(self):
        self.assertFalse(B.parse_minfo("cdrecord: No disk / Wrong disk!\n").present)

    def test_rw_written(self):
        m = B.parse_minfo(self.MINFO.replace("not erasable", "erasable").replace("status:              empty",
                                                                                  "status:              complete"))
        self.assertTrue(m.erasable)
        self.assertFalse(m.blank)

    def test_wav_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "a.wav")
            B.write_wav(p, S.ZeroSource(300, 2352))
            data = read_bytes(p)
            self.assertEqual(len(data), 44 + 300 * 2352)
            self.assertEqual(data[:4] + data[8:16], b"RIFFWAVEfmt ")
            self.assertEqual(struct.unpack_from("<HHI", data, 20), (1, 2, 44100))


if __name__ == "__main__":
    unittest.main()
