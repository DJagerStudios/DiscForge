"""Regression tests for the code review findings and the per-console exceptions research.
python3 -m unittest discover -s tests"""
import os
import re
import shutil
import struct
import subprocess
import sys
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from discforge import burner as B  # noqa: E402
from discforge import cue as CUE  # noqa: E402
from discforge import detect as D  # noqa: E402
from discforge import jobs as J  # noqa: E402
from discforge import quirks as Q  # noqa: E402
from discforge import util  # noqa: E402
from discforge import xbox as X  # noqa: E402
from discforge.engine import media_ok  # noqa: E402
from test_multi import SimDrive, Tmp, burn, raw_mode2  # noqa: E402

HAVE_CDRDAO = shutil.which("cdrdao") is not None
HAVE_MKISOFS = shutil.which("mkisofs") is not None


def show_toc(toc: str) -> tuple[list[int], int]:
    out = subprocess.run(["cdrdao", "show-toc", toc], capture_output=True, text=True).stdout
    starts = [int(m) for m in re.findall(r"^\s*START\s+\S+\(\s*(\d+)\)", out, re.M)]
    ends = [int(m) for m in re.findall(r"^\s*END\s+\S+\(\s*(\d+)\)", out, re.M)]
    return starts, ends[-1] if ends else 0


class CueLayouts(Tmp):
    """layout() must agree with what real cdrdao makes of the generated TOC."""

    def sheet(self, text: str, files: dict[str, int]) -> CUE.CueSheet:
        for name, frames in files.items():
            self.write(f"c/{name}", bytes(2352 * frames))
        return CUE.parse_cue(self.write("c/x.cue", text.encode()))

    def agree(self, sheet: CUE.CueSheet, want_lbas: list[int]):
        lay = [lba for _n, lba, _d in CUE.layout(sheet)]
        self.assertEqual(lay, want_lbas)
        if HAVE_CDRDAO:
            toc = self.p("c", "x.toc")
            CUE.write_toc(sheet, toc)
            starts, end = show_toc(toc)
            self.assertEqual(starts, want_lbas)
            self.assertEqual(end, sheet.total_frames)

    def test_track1_pregap_is_the_leadin(self):
        s = self.sheet('FILE "a.bin" BINARY\n TRACK 01 MODE2/2352\n  PREGAP 00:02:00\n  INDEX 01 00:00:00\n'
                       'FILE "b.bin" BINARY\n TRACK 02 AUDIO\n  INDEX 00 00:00:00\n  INDEX 01 00:02:00\n',
                       {"a.bin": 400, "b.bin": 450})
        self.agree(s, [0, 550])

    def test_data_track1_stored_index0_is_skipped(self):
        s = self.sheet('FILE "a.bin" BINARY\n TRACK 01 MODE1/2352\n  INDEX 00 00:00:00\n  INDEX 01 00:02:00\n',
                       {"a.bin": 600})
        self.agree(s, [0])
        self.assertEqual(s.total_frames, 450)

    def test_audio_track1_hidden_track_kept(self):
        s = self.sheet('FILE "a.bin" BINARY\n TRACK 01 AUDIO\n  INDEX 00 00:00:00\n  INDEX 01 00:10:00\n',
                       {"a.bin": 1500})
        self.agree(s, [750])

    def test_pregap_and_index0_in_one_track(self):
        s = self.sheet('FILE "a.bin" BINARY\n TRACK 01 MODE1/2352\n  INDEX 01 00:00:00\n'
                       ' TRACK 02 AUDIO\n  PREGAP 00:01:00\n  INDEX 00 00:06:00\n  INDEX 01 00:08:00\n',
                       {"a.bin": 1000})
        self.agree(s, [0, 450 + 75 + 150])

    def test_eac_gaps_appended_to_previous_file(self):
        s = self.sheet('FILE "t1.bin" BINARY\n TRACK 01 MODE2/2352\n  INDEX 01 00:00:00\n'
                       ' TRACK 02 AUDIO\n  INDEX 00 00:05:00\n'
                       'FILE "t2.bin" BINARY\n  INDEX 01 00:00:00\n', {"t1.bin": 525, "t2.bin": 400})
        t2 = s.tracks[1]
        self.assertEqual((t2.pre_length, t2.start, t2.length), (150, 0, 400))
        self.agree(s, [0, 525])

    def test_cdi_uses_cd_i_toc(self):
        s = self.sheet('FILE "a.bin" BINARY\n TRACK 01 CDI/2352\n  INDEX 01 00:00:00\n', {"a.bin": 400})
        toc = self.p("c", "x.toc")
        CUE.write_toc(s, toc)
        with open(toc) as f:
            self.assertTrue(f.read().startswith("CD_I"))

    def test_file_paths_stay_next_to_the_cue(self):
        self.write("secret.bin", bytes(2352 * 300))
        self.write("c/x.cue", b'FILE "../secret.bin" BINARY\n TRACK 01 MODE1/2352\n  INDEX 01 00:00:00\n')
        with self.assertRaises(CUE.CueError):
            CUE.parse_cue(self.p("c", "x.cue"))

    def test_multisession_and_gdrom_cues_blocked(self):
        self.write("m/a.bin", bytes(2352 * 400))
        cue = self.write("m/x.cue", b'REM SESSION 01\nFILE "a.bin" BINARY\n TRACK 01 AUDIO\n  INDEX 01 00:00:00\n'
                                    b'REM SESSION 02\n TRACK 02 AUDIO\n  INDEX 01 00:03:00\n')
        self.assertTrue(any("multi-session" in b for b in D.identify(cue).blockers))
        cue2 = self.write("m/g.cue", b'REM SINGLE-DENSITY AREA\nFILE "a.bin" BINARY\n TRACK 01 MODE1/2352\n'
                                     b'  INDEX 01 00:00:00\nREM HIGH-DENSITY AREA\n')
        ident = D.identify(cue2)
        self.assertEqual(ident.platform, "dreamcast")
        self.assertTrue(any(".gdi" in b for b in ident.blockers))

    def test_lone_bin_with_audio_needs_its_cue(self):
        self.write("l/game.bin", raw_mode2(bytes(2048 * 300)) + os.urandom(2352 * 300))
        ident = D.identify(self.p("l", "game.bin"))
        self.assertTrue(any(".cue" in b for b in ident.blockers))


@unittest.skipUnless(HAVE_MKISOFS, "mkisofs not installed")
class Jobs(Tmp):
    def test_folder_with_cue_bin_is_a_playstation_game(self):
        os.makedirs(self.p("folder"))
        cue = self.ps1_disc(audio=False)
        for n in os.listdir(os.path.dirname(cue)):
            shutil.copy(os.path.join(os.path.dirname(cue), n), self.p("folder", n))
        job = J.open_job(self.p("folder"))
        self.assertEqual((job.platform.id, job.method), ("ps1", "cd"))
        job.cleanup()

    def test_ecm_cue_inside_zip(self):
        cue = self.ps1_disc(audio=False)
        b = self.p("ps1", "Game (Track 1).bin")
        from test_ecm import Encoder
        enc = Encoder()
        with open(b, "rb") as f:
            enc.mode2_bin(f.read())
        with open(b + ".ecm", "wb") as f:
            f.write(enc.finish())
        z = self.p("Game (Europe).zip")
        with zipfile.ZipFile(z, "w") as zf:
            zf.write(cue, "Game (USA).cue")
            zf.write(b + ".ecm", "Game (Track 1).bin.ecm")
        job = J.open_job(z)
        self.assertEqual((job.platform.id, job.method, job.game_id), ("ps1", "cd", "SLUS-01234"))
        self.assertFalse(job.blockers)
        job.cleanup()

    def test_toc_check_rejects_a_bad_layout(self):
        if not HAVE_CDRDAO:
            self.skipTest("cdrdao not installed")
        toc = self.write("t/bad.toc", b'CD_ROM\nTRACK MODE1\nDATAFILE "missing.iso" 00:10:00\n')
        self.assertIsNotNone(J.check_toc(toc, [(1, 0, True)], 750))

    def test_user_toc_burns_from_its_folder_and_is_not_called_verified(self):
        self.write("t/data.iso", bytes(2048 * 400))
        toc = self.write("t/disc.toc", b'CD_ROM\nTRACK MODE1\nDATAFILE "data.iso" 00:05:25\n')
        job = J.open_job(toc)
        self.assertEqual(job.cd.cwd, self.p("t"))
        with SimDrive(self.tmp) as sim:
            res = burn(job, sim)
            self.assertTrue(res.ok)
            self.assertIsNone(res.verified)

    def test_edc_protected_game_never_falls_back_to_cooked(self):
        os.makedirs(self.p("ddr"))
        cue = self.ps1_disc(audio=False)
        for n in os.listdir(os.path.dirname(cue)):
            dst = n.replace("Game (USA)", "Dance Dance Revolution (USA)")
            shutil.copy(os.path.join(os.path.dirname(cue), n), self.p("ddr", dst))
        with open(self.p("ddr", "Dance Dance Revolution (USA).cue"), "w") as f:
            f.write('FILE "Game (Track 1).bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n')
        job = J.open_job(self.p("ddr", "Dance Dance Revolution (USA).cue"))
        self.assertTrue(job.cd.raw_only)
        with SimDrive(self.tmp, raw_unsupported=True) as sim:
            with self.assertRaises(B.BurnError) as cm:
                burn(job, sim)
            self.assertIn("raw", str(cm.exception))

    def test_old_cdrdao_refused_for_raw_audio(self):
        job = J.open_job(self.ps1_disc())
        with SimDrive(self.tmp, version="1.2.4") as sim:
            with self.assertRaises(B.BurnError) as cm:
                burn(job, sim)
            self.assertIn("1.2.5", str(cm.exception))

    def test_verify_without_raw_reads_or_readcd_is_not_called_checked(self):
        from discforge import cdraw
        job = J.open_job(self.ps1_disc(audio=False))
        with SimDrive(self.tmp) as sim:
            class NoRaw:
                def __init__(self, dev):
                    raise OSError("no SG_IO here")
            cdraw.RawCd = NoRaw
            saved = cdraw.tools.find
            cdraw.tools.find = lambda name: None if name in ("readcd", "readom") else saved(name)
            try:
                res = burn(job, sim)
            finally:
                cdraw.tools.find = saved
            self.assertTrue(res.ok)
            self.assertIsNone(res.verified)
            self.assertTrue(res.messages)


class Consoles(Tmp):
    def test_ps2_dvd9_rules(self):
        self.assertIsNone(Q.ps2_layer_problem(1_622_400, 2_763_936))          # Gran Turismo 4: fine
        self.assertIn("second layer", Q.ps2_layer_problem(1_902_128, 3_915_344))   # God of War: L1 > L0
        self.assertIn("first layer", Q.ps2_layer_problem(2_087_456, 4_100_000))    # Rock Band 2 (EU)

    def test_media_rules(self):
        cdrw = B.Media(present=True, kind="CD-RW", blank=True, erasable=True, capacity=359849)
        self.assertIn("CD-RW", media_ok(cdrw, "cd", 1000, "ps1"))
        self.assertIsNone(media_ok(cdrw, "cd", 1000, "pc"))
        dashdl = B.Media(present=True, kind="DVD-R/DL", blank=True, capacity=4171712)
        self.assertIn("DVD+R DL", media_ok(dashdl, "dvd-dl", 4_000_000, "ps2", plus_dl_only=True))
        plus = B.Media(present=True, kind="DVD+R", blank=True, capacity=2295104)
        self.assertIn("DVD-R", media_ok(plus, "dvd", 2_298_496, "wii"))

    def test_wii_dual_layer_break(self):
        h = bytearray(0x440)
        h[0:6] = b"RSBE01"
        h[0x18:0x1C] = D.WII_MAGIC
        path = self.p("brawl.iso")
        with open(path, "wb") as f:
            f.write(h)
            f.truncate(Q.WII_DL_SECTORS * 2048)
        ident = D.identify(path)
        self.assertEqual((ident.media, ident.layer_break, ident.plus_dl_only), ("dvd-dl", Q.WII_DL_BREAK, True))

    def test_gamecube_audio_streaming_warning(self):
        h = bytearray(0x440)
        h[0:6] = b"GIKP70"
        h[0x1C:0x20] = D.GC_MAGIC
        h[8] = 1
        path = self.p("Ikaruga (Europe).iso")
        with open(path, "wb") as f:
            f.write(h)
            f.truncate(1_459_978_240)
        self.assertTrue(any("streams its music" in w for w in D.identify(path).warnings))

    def test_ps1_protection_table(self):
        self.assertIn("EDC", Q.ps1_protection("Dance Dance Revolution Konamix (USA)"))
        self.assertTrue(any(t.startswith("APv") for t in Q.ps1_protection("Spyro: Year of the Dragon (USA)")) or
                        Q.ps1_protection("Spyro - Year of the Dragon (Europe)"))

    def test_xbox_media_patch(self):
        xbe = bytearray(0x2000)
        xbe[0:4] = b"XBEH"
        struct.pack_into("<I", xbe, 0x104, 0x10000)
        struct.pack_into("<I", xbe, 0x118, 0x10200)
        xbe[0x1000:0x1008] = Q.XBE_MEDIA_PATTERN
        self.write("x/default.xbe", bytes(xbe))
        self.write("x/data.bin", b"hello")
        out = self.p("x.xiso")
        _size, patched = X.folder_to_xiso(self.p("x"), out)
        self.assertEqual(patched, 1)
        with open(out, "rb") as f:
            data = f.read()
        self.assertNotIn(Q.XBE_MEDIA_PATTERN, data)
        self.assertIn(Q.XBE_MEDIA_PATTERN[:-1] + b"\xeb", data)

    def test_default_xbe_opens(self):
        xbe = bytearray(0x1000)
        xbe[0:4] = b"XBEH"
        self.write("xb/default.xbe", bytes(xbe))
        self.write("xb/media/a.bin", os.urandom(3000))
        job = J.open_job(self.p("xb", "default.xbe"))
        self.assertEqual((job.platform.id, job.method), ("xbox", "dvd"))
        job.cleanup()


class CacheLocks(Tmp):
    def test_folders_in_use_survive_cleanups(self):
        a = self.write("src/a.iso", b"a" * 4096)
        b = self.write("src/b.iso", b"b" * 4096)
        da, _done, lock = J.conversion_dir(a)
        db, _d2, lock_b = J.conversion_dir(b)              # converting b would delete a's folder...
        self.assertTrue(os.path.isdir(da))                  # ...but a is locked
        lock.release()
        lock_b.release()
        os.utime(da, (0, 0))
        util.clean_cache(1)
        self.assertFalse(os.path.isdir(da))


if __name__ == "__main__":
    unittest.main()
