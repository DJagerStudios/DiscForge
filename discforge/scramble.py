"""1ST_READ.BIN scrambling (MIL-CD boot) - port of KallistiOS utils/scramble.

When the Dreamcast boots a CD-ROM (MIL-CD) instead of a GD-ROM, the boot ROM
descrambles 1ST_READ.BIN while loading it, so selfboot discs must carry a
scrambled binary.  GD-ROM dumps and dc-load style binaries are unscrambled.
"""
from __future__ import annotations

import numpy as np

MAXCHUNK = 2048 * 1024
LOAD_ADDR = 0x8C010000


class _Rng:
    def __init__(self, n: int):
        self.seed = n & 0xFFFF

    def next(self) -> int:
        self.seed = (self.seed * 2109 + 9273) & 0x7FFF
        return (self.seed + 0xC000) & 0xFFFF


def _chunk_order(rng: _Rng, slices: int) -> list[int]:
    """Order in which the slices of one chunk are stored in the scrambled file."""
    idx = list(range(slices))
    order = []
    for i in range(slices - 1, -1, -1):
        x = (rng.next() * i) >> 16
        idx[i], idx[x] = idx[x], idx[i]
        order.append(idx[i])
    return order


def _plan(size: int):
    rng = _Rng(size)
    pos = 0
    chunk = MAXCHUNK
    while chunk >= 32:
        while size - pos >= chunk:
            yield pos, chunk, _chunk_order(rng, chunk // 32)
            pos += chunk
        chunk >>= 1
    # trailing bytes (< 32) are stored as is


def scramble(data: bytes) -> bytes:
    out = bytearray(len(data))
    w = 0
    for pos, _chunk, order in _plan(len(data)):
        for s in order:
            out[w:w + 32] = data[pos + 32 * s:pos + 32 * s + 32]
            w += 32
    out[w:] = data[w:]
    return bytes(out)


def descramble(data: bytes) -> bytes:
    out = bytearray(len(data))
    r = 0
    for pos, _chunk, order in _plan(len(data)):
        for s in order:
            out[pos + 32 * s:pos + 32 * s + 32] = data[r:r + 32]
            r += 32
    out[r:] = data[r:]
    return bytes(out)


def sh4_literal_score(data: bytes) -> float:
    """How much `data` looks like SH4 code loaded at 0x8C010000.

    SH4 code loads constants with `mov.l @(disp,PC),Rn` (0xDnxx); in a real
    binary a large share of those constants are pointers into main RAM.  The
    targets sit up to 1 KB away, so once 32-byte slices are shuffled they
    point at unrelated words instead.
    """
    n = len(data) & ~3
    if n < 64:
        return 0.0
    buf = np.frombuffer(data[:n], dtype="<u2")
    words = np.frombuffer(data[:n], dtype="<u4")
    pcs = np.nonzero((buf & 0xF000) == 0xD000)[0] * 2
    if len(pcs) == 0:
        return 0.0
    disp = (buf[pcs // 2] & 0xFF).astype(np.int64)
    target = (pcs & ~3) + 4 + disp * 4
    target = target[target + 4 <= n]
    if len(target) == 0:
        return 0.0
    vals = words[target // 4]
    hits = np.count_nonzero((vals >= 0x8C000000) & (vals < 0x8D000000))
    return hits / len(target)


def _vote(plain: float, unscr: float) -> bool | None:
    if unscr > plain * 1.6 and unscr > 0.04:
        return True
    if plain > unscr * 1.6 and plain > 0.04:
        return False
    return None


def scramble_votes(data: bytes) -> tuple[bool | None, bool | None]:
    """(vote from the first KB, vote from the whole file): True = scrambled, False = plain."""
    if len(data) < 256:
        return None, None
    desc = descramble(data)
    head = _vote(sh4_literal_score(data[:1024]), sh4_literal_score(desc[:1024]))
    whole = _vote(sh4_literal_score(data), sh4_literal_score(desc))
    return head, whole


def looks_scrambled(data: bytes) -> bool | None:
    """True = scrambled, False = plain, None = can't tell.

    Two votes: the first KB (entry code / unpacker stub, decisive even for
    packed executables) and the whole file (decisive for normal binaries).
    """
    votes = [v for v in scramble_votes(data) if v is not None]
    if not votes or (len(votes) == 2 and votes[0] != votes[1]):
        return None
    return votes[0]
