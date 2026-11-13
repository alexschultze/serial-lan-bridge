import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tether_host import protocol as P  # noqa: E402
from tether_host.protocol import Deframer, Frame, T, cobs_decode, cobs_encode, encode  # noqa: E402

VECTORS = os.path.join(os.path.dirname(__file__), "..", "..", "testvectors", "frames.json")


class Framing(unittest.TestCase):
    def test_cobs_roundtrip(self):
        for data in (b"", b"\x00", b"\x00\x00", b"abc", bytes(range(256)) * 3, b"\xff" * 254, b"\xff" * 255, b"a" * 600):
            enc = cobs_encode(data)
            self.assertNotIn(0, enc)
            self.assertEqual(cobs_decode(enc), data)

    def test_frames_split_arbitrarily(self):
        frames = [Frame(T.DATA, 7, os.urandom(1000)), Frame(T.CLOSE, 7, b"\x00"), Frame(T.PING, 0, b"\x01\x00\x00\x00")]
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

    def test_max_payload_roundtrip_and_limit(self):
        f = Frame(T.DATA, 16, bytes([0]) * P.MAX_PAYLOAD)  # worst case for COBS
        wire = encode(f)
        self.assertLessEqual(len(wire) - 1, P.MAX_RAW)
        self.assertEqual(list(Deframer().feed(wire))[0].payload, f.payload)
        with self.assertRaises(P.FrameError):
            encode(Frame(T.DATA, 1, bytes(P.MAX_PAYLOAD + 1)))

    def test_oversized_garbage_is_bounded(self):
        d = Deframer()
        out = list(d.feed(b"\x01" * 5000 + b"\x00" + encode(Frame(T.PONG, 0, b"\0\0\0\0"))))
        self.assertEqual([f.type for f in out], [T.PONG])
        self.assertEqual(d.errors, 1)


class Payloads(unittest.TestCase):
    def test_hello(self):
        f = P.hello(P.Role.NODE, P.NodeState.NEEDS_CONFIG, 0xDEADBEEF, "tether-node 0.1")
        h = P.parse_hello(f.payload)
        self.assertEqual((h["proto"], h["role"], h["state"], h["session"], h["version"]),
                         (1, P.Role.NODE, P.NodeState.NEEDS_CONFIG, 0xDEADBEEF, "tether-node 0.1"))

    def test_accept_credit_close(self):
        self.assertEqual(P.parse_accept(P.accept(3, 1, bytes([192, 168, 1, 7]), 51234).payload),
                         (1, bytes([192, 168, 1, 7]), 51234))
        self.assertEqual(P.parse_credit(P.credit(3, 2048).payload), 2048)
        self.assertEqual(P.parse_close(P.close(3, P.Close.RESET).payload), P.Close.RESET)

    def test_open(self):
        f = P.open_req(P.HOST_CHAN_BASE + 2, 9)
        self.assertEqual((f.type, f.chan, P.parse_open(f.payload)), (P.T.OPEN, 0x8002, 9))
        self.assertEqual(P.parse_open_res(P.open_res(0x8002, P.Open.DNS).payload), P.Open.DNS)
        with self.assertRaises(P.FrameError):
            P.parse_open(b"\x01")

    def test_data_split(self):
        frames = list(P.data_frames(2, bytes(2500)))
        self.assertEqual([len(f.payload) for f in frames], [1024, 1024, 452])

    def test_config_chunking(self):
        blob = json.dumps({"v": 1, "pad": "x" * 3000}).encode()
        frames = list(P.config_frames(blob))
        self.assertEqual([bool(f.flags & P.FLAG_MORE) for f in frames], [True] * (len(frames) - 1) + [False])
        self.assertEqual(len(frames), 3)
        asm, out = P.ConfigAssembler(), None
        for f in frames:
            out = asm.add(f)
        self.assertEqual(out, blob)

    def test_config_too_large(self):
        with self.assertRaises(P.FrameError):
            list(P.config_frames(bytes(P.MAX_CONFIG + 1)))


class Vectors(unittest.TestCase):
    """testvectors/frames.json is shared with the C implementation in tether-node."""

    def test_vectors_decode_and_reencode(self):
        with open(VECTORS) as fh:
            vectors = json.load(fh)
        self.assertGreater(len(vectors), 5)
        for v in vectors:
            wire = bytes.fromhex(v["wire"])
            got = list(Deframer().feed(wire))
            self.assertEqual(len(got), 1, v["name"])
            f = got[0]
            self.assertEqual((f.type, f.flags, f.chan, f.payload.hex()), (v["type"], v["flags"], v["chan"], v["payload"]), v["name"])
            self.assertEqual(encode(f), wire, v["name"])


if __name__ == "__main__":
    unittest.main()
