"""LibCrypt patching: PPF parsing, revision checks, EDC/ECC validation, never touching the source."""
import hashlib
import os
import shutil
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from discforge import cue as CUE  # noqa: E402
from discforge import jobs as J  # noqa: E402
from discforge import libcrypt as L  # noqa: E402
from discforge import sectors as S  # noqa: E402
from test_multi import mkiso, raw_mode2  # noqa: E402

HAVE_MKISOFS = shutil.which("mkisofs") is not None


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def chunks(records):
    """PPF records hold at most 255 bytes."""
    for off, new, old in records:
        for k in range(0, len(new), 255):
            yield off + k, new[k:k + 255], old[k:k + 255] if old is not None else None


def ppf3(records, undo=True, block=None):
    head = b"PPF30" + b"\x02" + b"Test patch".ljust(50) + bytes([0, 1 if block else 0, 1 if undo else 0, 0])
    if block:
        head += block
    body = b""
    for off, new, old in chunks(records):
        body += struct.pack("<QB", off, len(new)) + new + (old if undo else b"")
    return head + body


def ppf1(records):
    out = b"PPF10" + b"\x00" + b"Test patch".ljust(50)
    for off, new, _old in chunks(records):
        out += struct.pack("<IB", off, len(new)) + new
    return out


class LibCryptData(unittest.TestCase):
    def test_every_patch_parses(self):
        idx = L._index()
        self.assertEqual(len(idx["games"]), 229)
        names = {v["ppf"] for e in idx["games"].values() for v in e["variants"] if v.get("ppf")}
        self.assertEqual(len(names), 221)
        for n in names:
            p = L.parse_ppf(L._ppf_bytes(n))
            self.assertTrue(p.records, n)
            self.assertTrue(all(len(new) for _o, new, _u in p.records), n)

    def test_known_titles(self):
        self.assertIn("MediEvil", L.lookup("SCES_003.11")["title"])
        self.assertIn("Final Fantasy VIII", L.lookup("SLES_320.80")["title"])
        self.assertIsNone(L.lookup("SLUS_007.00"))


@unittest.skipUnless(HAVE_MKISOFS, "mkisofs not installed")
class LibCryptPatching(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="df-lc-")
        self.env = dict(os.environ)
        os.environ["XDG_CACHE_HOME"] = os.path.join(self.tmp, "cache")
        src = os.path.join(self.tmp, "src")
        os.makedirs(src)
        with open(os.path.join(src, "SYSTEM.CNF"), "wb") as f:
            f.write(b"BOOT = cdrom:\\SLES_999.99;1\r\nTCB = 4\r\n")
        with open(os.path.join(src, "SLES_999.99"), "wb") as f:
            f.write(os.urandom(120_000))
        iso = mkiso(src, os.path.join(self.tmp, "g.iso"), "-sysid", "PLAYSTATION")
        with open(iso, "rb") as f:
            self.raw = bytearray(raw_mode2(f.read()))
        self.bin = os.path.join(self.tmp, "Game (Europe).bin")
        with open(self.bin, "wb") as f:
            f.write(self.raw)
        self.cue = os.path.join(self.tmp, "Game (Europe).cue")
        with open(self.cue, "w") as f:
            f.write('FILE "Game (Europe).bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n')
        self.saved = (L._index, L._ppf_bytes)
        self.sector = 30
        base = self.sector * 2352
        old_user = bytes(self.raw[base + 124:base + 128])
        new_sector = bytearray(self.raw[base:base + 2352])
        new_sector[124:128] = b"\x00\x00\x00\x00" if old_user != b"\0\0\0\0" else b"\x01\x02\x03\x04"
        new_sector[16:] = S.encode_mode2_form1(bytes(new_sector[24:2072]), bytes(new_sector[16:24]))
        self.new_sector = bytes(new_sector)
        self.recs_full = [(base + 124, bytes(new_sector[124:128]), old_user),
                          (base + 2072, bytes(new_sector[2072:2352]), bytes(self.raw[base + 2072:base + 2352]))]
        self.recs_user_only = self.recs_full[:1]

    def tearDown(self):
        L._index, L._ppf_bytes = self.saved
        os.environ.clear()
        os.environ.update(self.env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def use(self, ppf: bytes, variants=None):
        idx = {"games": {"SLES_999.99": {"title": "Test Game (Europe)",
                                         "variants": variants or [{"ppf": "T.PPF"}]}}}
        L._index = lambda: idx
        L._ppf_bytes = lambda name: ppf

    def test_boot_file(self):
        self.assertEqual(CUE.ps_boot_file(CUE.parse_cue(self.cue)), "SLES_999.99")

    def test_patch_with_checksums_and_undo(self):
        self.use(ppf3(self.recs_full))
        before = sha(self.bin)
        out = os.path.join(self.tmp, "out.bin")
        res = L.patch_image(self.bin, out, "SLES_999.99")
        self.assertEqual(res.status, "patched")
        self.assertEqual(res.fixed, 0)
        self.assertEqual(sha(self.bin), before)                 # the original is never modified
        with open(out, "rb") as f:
            data = f.read()
        self.assertEqual(data[self.sector * 2352:(self.sector + 1) * 2352], self.new_sector)
        self.assertEqual(L.patch_image(out, out + "2", "SLES_999.99").status, "already")

    def test_missing_checksums_are_recomputed(self):
        self.use(ppf1(self.recs_user_only))
        out = os.path.join(self.tmp, "out.bin")
        res = L.patch_image(self.bin, out, "SLES_999.99")
        self.assertEqual((res.status, res.fixed), ("patched", 1))
        with open(out, "rb") as f:
            f.seek(self.sector * 2352)
            self.assertEqual(f.read(2352), self.new_sector)

    def test_wrong_revision_checksums_refused(self):
        bad = [(o, n, u) for o, n, u in self.recs_full]
        bad[1] = (bad[1][0], bytes(b ^ 0x5A for b in bad[1][1]), bad[1][2])
        self.use(ppf1(bad))
        out = os.path.join(self.tmp, "out.bin")
        self.assertEqual(L.patch_image(self.bin, out, "SLES_999.99").status, "mismatch")
        self.assertFalse(os.path.exists(out) or os.path.exists(out + ".part"))

    def test_undo_mismatch_refused(self):
        recs = [(o, n, bytes(b ^ 0xFF for b in u)) for o, n, u in self.recs_full]
        self.use(ppf3(recs))
        self.assertEqual(L.patch_image(self.bin, os.path.join(self.tmp, "o.bin"), "SLES_999.99").status, "mismatch")

    def test_revision_byte_selects_variant_or_refuses(self):
        off = self.sector * 2352 + 500
        byte = self.raw[off]
        variants = [{"check": [off, byte], "ppf": "T.PPF", "title": "v1.0"},
                    {"check": [off, (byte + 1) % 256], "ppf": None, "title": "v1.1", "unprotected": True}]
        self.use(ppf3(self.recs_full), variants)
        self.assertEqual(L.patch_image(self.bin, os.path.join(self.tmp, "a.bin"), "SLES_999.99").status, "patched")
        variants[0]["check"][1] = (byte + 2) % 256
        variants[1]["check"][1] = (byte + 3) % 256
        self.assertEqual(L.patch_image(self.bin, os.path.join(self.tmp, "b.bin"), "SLES_999.99").status,
                         "unknown-revision")

    def test_job_patches_automatically_and_can_be_turned_off(self):
        self.use(ppf3(self.recs_full))
        job = J.open_job(self.cue)
        self.assertIn(("LibCrypt", "protection removed automatically"), job.details)
        data_file = job.cd.verify[0].file
        self.assertTrue(data_file.endswith("libcrypt-patched.bin"))
        with open(job.cd.toc) as f:
            self.assertNotIn("Game (Europe).bin", f.read())
        job.cleanup()
        job = J.open_job(self.cue, opts={"libcrypt": False})
        self.assertTrue(any("LibCrypt" in w for w in job.warnings))
        self.assertEqual(job.cd.verify[0].file, os.path.abspath(self.bin))
        job.cleanup()


if __name__ == "__main__":
    unittest.main()
