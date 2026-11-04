"""Native ECM decoding: byte-exact against a reference encoder (and bin2ecm when installed)."""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import numpy as np                                   # noqa: E402
from discforge import formats as F, sectors as S     # noqa: E402
from test_multi import raw_mode2                     # noqa: E402

SYNC = b"\x00" + b"\xff" * 10 + b"\x00"


def _crc_table():
    t = []
    for i in range(256):
        e = i
        for _ in range(8):
            e = (e >> 1) ^ (0xD8018001 if e & 1 else 0)
        t.append(e)
    return t


_T = _crc_table()


def cd_edc(data: bytes, crc: int = 0) -> int:
    for b in data:
        crc = (crc >> 8) ^ _T[(crc ^ b) & 0xFF]
    return crc


def bcd(n):
    return ((n // 10) << 4) | (n % 10)


def mode1(lba: int, user: bytes) -> bytes:
    raw = np.zeros((1, 2352), dtype=np.uint8)
    raw[0, :12] = np.frombuffer(SYNC, dtype=np.uint8)
    a = lba + 150
    raw[0, 12:16] = [bcd(a // 4500), bcd(a // 75 % 60), bcd(a % 75), 1]
    raw[0, 16:2064] = np.frombuffer(user, dtype=np.uint8)
    raw[0, 2064:2068] = np.frombuffer(cd_edc(raw[0, :2064].tobytes()).to_bytes(4, "little"), dtype=np.uint8)
    raw[0, 2076:2248] = S._ecc_block(raw[:, 12:2076], S._P_IDX, 86)[0]
    raw[0, 2248:2352] = S._ecc_block(raw[:, 12:2248], S._Q_IDX, 52)[0]
    return raw.tobytes()


def form2(sector: bytes) -> bytes:
    sec = bytearray(sector)
    sec[16:24] = bytes([0, 0, 0x20, 0]) * 2
    sec[24:2348] = os.urandom(2324)
    sec[2348:2352] = cd_edc(bytes(sec[16:2348])).to_bytes(4, "little")
    return bytes(sec)


class Encoder:
    """A small ECM writer (same record format as Neill Corlett's ecm.c)."""

    def __init__(self):
        self.out = bytearray(b"ECM\0")
        self.plain = bytearray()

    def _count(self, kind: int, count: int):
        count = (count - 1) & 0xFFFFFFFF
        self.out.append(((count >= 32) << 7) | ((count & 31) << 2) | kind)
        count >>= 5
        while count:
            self.out.append(((count >= 128) << 7) | (count & 127))
            count >>= 7

    def literal(self, data: bytes):
        self._count(0, len(data))
        self.out += data
        self.plain += data

    def sectors(self, kind: int, secs: list):
        self._count(kind, len(secs))
        for s in secs:
            if kind == 1:
                self.out += s[12:15] + s[16:2064]
                self.plain += s
            else:                          # Mode 2: only the 2336 bytes after sync+header are typed
                body = s[-2336:]
                self.out += body[4:2056] if kind == 2 else body[4:2332]
                self.plain += body

    def mode2_bin(self, image: bytes):
        """Each 2352-byte Mode 2 sector: its 16 header bytes as literal, the rest as a typed record."""
        for i in range(0, len(image), 2352):
            s = image[i:i + 2352]
            self.literal(s[:16])
            self.sectors(3 if s[18] & 0x20 else 2, [s])

    def finish(self) -> bytes:
        self._count(0, 0x100000000)        # count-1 == 0xFFFFFFFF: end of records
        self.out += cd_edc(bytes(self.plain)).to_bytes(4, "little")
        return bytes(self.out)


class NativeEcm(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="df-ecm-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _decode(self, blob: bytes) -> bytes:
        src, dst = os.path.join(self.tmp, "x.bin.ecm"), os.path.join(self.tmp, "x.bin")
        with open(src, "wb") as f:
            f.write(blob)
        F.ecm_decode_native(src, dst)
        with open(dst, "rb") as f:
            return f.read()

    def _mixed(self) -> Encoder:
        e = Encoder()
        img = bytearray(raw_mode2(os.urandom(2048 * 40)))
        for i in range(10, 16):
            img[i * 2352:(i + 1) * 2352] = form2(img[i * 2352:(i + 1) * 2352])
        e.mode2_bin(bytes(img))
        e.sectors(1, [mode1(1000 + i, os.urandom(2048)) for i in range(7)])
        e.literal(os.urandom(2352 * 5 + 17))                          # audio / odd bytes
        e.sectors(2, [raw_mode2(os.urandom(2048))[16:] for _ in range(4)])   # MODE2/2336 run
        return e

    def test_mixed_image_round_trip(self):
        e = self._mixed()
        self.assertEqual(self._decode(e.finish()), bytes(e.plain))

    def test_small_batches_and_split_records(self):
        e = self._mixed()
        blob = e.finish()
        with mock.patch.multiple(F, _ECM_LIT_STEP=1000, _ECM_SEC_STEP=3, _ECM_BATCH=5000):
            self.assertEqual(self._decode(blob), bytes(e.plain))

    def test_long_counts(self):
        e = Encoder()
        e.literal(os.urandom(300_000))           # needs three continuation bytes
        self.assertEqual(self._decode(e.finish()), bytes(e.plain))

    def test_damage_is_reported(self):
        e = self._mixed()
        blob = bytearray(e.finish())
        bad = bytearray(blob)
        bad[len(bad) // 2] ^= 0x40
        with self.assertRaisesRegex(F.FormatError, "damaged"):
            self._decode(bytes(bad))
        with self.assertRaisesRegex(F.FormatError, "ends early"):
            self._decode(bytes(blob[:len(blob) // 2]))
        with self.assertRaisesRegex(F.FormatError, "isn't an ECM"):
            self._decode(b"NOPE" + bytes(blob[4:]))
        with self.assertRaisesRegex(F.FormatError, "ends early"):
            self._decode(bytes(blob[:-2]))
        with self.assertRaisesRegex(F.FormatError, "damaged"):
            self._decode(b"ECM\0" + b"\xff" * 8)          # count that never ends

    def test_used_when_no_tool_is_installed(self):
        e = Encoder()
        e.mode2_bin(raw_mode2(os.urandom(2048 * 3)))
        src, dst = os.path.join(self.tmp, "g.bin.ecm"), os.path.join(self.tmp, "g.bin")
        with open(src, "wb") as f:
            f.write(e.finish())
        with mock.patch.object(F.tools, "find", return_value=None):
            F.ecm_decode(src, dst)
        with open(dst, "rb") as f:
            self.assertEqual(f.read(), bytes(e.plain))

    @unittest.skipUnless(shutil.which("bin2ecm") or shutil.which("ecm"), "bin2ecm not installed")
    def test_matches_bin2ecm(self):
        img = bytearray(raw_mode2(os.urandom(2048 * 200)))
        for i in range(50, 70):
            img[i * 2352:(i + 1) * 2352] = form2(img[i * 2352:(i + 1) * 2352])
        img += b"".join(mode1(5000 + i, os.urandom(2048)) for i in range(30))
        img += os.urandom(2352 * 20)
        src = os.path.join(self.tmp, "t.bin")
        with open(src, "wb") as f:
            f.write(img)
        subprocess.run([shutil.which("bin2ecm") or shutil.which("ecm"), src, src + ".ecm"],
                       check=True, capture_output=True)
        F.ecm_decode_native(src + ".ecm", src + ".out")
        with open(src + ".out", "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).digest(), hashlib.sha256(img).digest())


if __name__ == "__main__":
    unittest.main()
