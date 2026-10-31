import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "host"))
from serialbridge.protocol import Deframer, Frame, T, cobs_decode, cobs_encode, encode  # noqa: E402


class Protocol(unittest.TestCase):
    def test_cobs_roundtrip(self):
        for data in (b"", b"\x00", b"\x00\x00", b"abc", bytes(range(256)) * 3, b"\xff" * 254, b"\xff" * 255, b"a" * 600):
            enc = cobs_encode(data)
            self.assertNotIn(0, enc)
            self.assertEqual(cobs_decode(enc), data)

    def test_frames_split_arbitrarily(self):
        frames = [Frame(T.DATA, 7, os.urandom(1000)), Frame(T.CLOSE, 7), Frame(T.PING)]
        wire = b"".join(encode(f) for f in frames)
        d = Deframer()
        got = []
        for i in range(0, len(wire), 13):
            got += list(d.feed(wire[i:i + 13]))
        self.assertEqual([(g.type, g.chan, g.payload) for g in got], [(f.type, f.chan, f.payload) for f in frames])
        self.assertEqual(d.errors, 0)

    def test_corruption_dropped_and_resync(self):
        good = encode(Frame(T.DATA, 1, b"hello"))
        bad = bytearray(encode(Frame(T.DATA, 2, b"world")))
        bad[4] ^= 0x55
        d = Deframer()
        got = list(d.feed(b"junk" + bytes(bad) + good))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].payload, b"hello")
        self.assertGreaterEqual(d.errors, 1)


if __name__ == "__main__":
    unittest.main()
