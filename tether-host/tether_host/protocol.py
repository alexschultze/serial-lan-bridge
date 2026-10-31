"""Wire protocol between tether-host (PC) and tether-node (ESP32-S3).

Normative description: ARCHITECTURE.md, section "Wire protocol". This module is the reference
implementation; the C implementation in tether-node must reproduce testvectors/frames.json.

Frame on the wire:  COBS( type u8 | flags u8 | chan u16le | payload | crc32 u32le ) 0x00
The CRC (zlib/IEEE CRC-32) covers everything before it. 0x00 never occurs inside an encoded
frame, so a receiver that loses sync recovers at the next delimiter.
"""
import struct
import zlib
from dataclasses import dataclass
from enum import IntEnum

PROTO_VERSION = 1
MAX_PAYLOAD = 1024          # bytes of payload per frame
MAX_BODY = 4 + MAX_PAYLOAD + 4
MAX_RAW = MAX_BODY + MAX_BODY // 254 + 2   # COBS-encoded size limit, without the delimiter
MAX_CHANNELS = 16           # data channels are 1..MAX_CHANNELS; channel 0 is control
MAX_LISTENERS = 4
DEFAULT_WINDOW = 4096       # per channel and direction, bytes
MAX_CONFIG = 8192           # reassembled CONFIG JSON, bytes
CONTROL_CHAN = 0

FLAG_MORE = 0x01            # CONFIG only: more chunks follow


class T(IntEnum):
    # control (chan 0)
    HELLO = 0x01        # both
    PING = 0x02         # host -> node, payload: seq u32
    PONG = 0x03         # node -> host, payload: seq u32 (echo)
    CONFIG = 0x10       # host -> node, JSON, may be chunked with FLAG_MORE
    STATUS = 0x11       # node -> host, JSON
    CONFIG_RES = 0x12   # node -> host, JSON
    LOG = 0x13          # node -> host, payload: level u8 | UTF-8 text
    REBOOT = 0x14       # host -> node, payload: mode u8 (0 app, 1 ROM download mode)
    # channels (chan 1..16)
    ACCEPT = 0x20       # node -> host, payload: listener u16 | peer ip4 | peer port u16
    DATA = 0x30         # both, payload: stream bytes (<= MAX_PAYLOAD)
    CREDIT = 0x31       # both, payload: u32 additional bytes the peer may send
    CLOSE = 0x32        # both, payload: reason u8
    # 0x40-0x4F reserved (UDP / outbound connections, later)


class Role(IntEnum):
    HOST = 0
    NODE = 1


class NodeState(IntEnum):
    NEEDS_CONFIG = 0
    RUNNING = 1


class Close(IntEnum):
    EOF = 0         # sender will send no more data (half-close); channel ends when both sent it
    RESET = 1       # abort now, both directions; receiver frees the channel, no reply
    PROTO = 2       # protocol violation (e.g. credit overrun); same effect as RESET
    NO_CHANNEL = 3  # reply to a frame for a channel that is not open; never answered


@dataclass
class Frame:
    type: int
    chan: int = 0
    payload: bytes = b""
    flags: int = 0


class FrameError(ValueError):
    pass


def cobs_encode(data: bytes) -> bytes:
    out = bytearray()
    out.append(0)  # placeholder for first code
    code_pos, code = 0, 1
    for b in data:
        if b == 0:
            out[code_pos] = code
            code_pos, code = len(out), 1
            out.append(0)
        else:
            out.append(b)
            code += 1
            if code == 0xFF:
                out[code_pos] = code
                code_pos, code = len(out), 1
                out.append(0)
    out[code_pos] = code
    return bytes(out)


def cobs_decode(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        code = data[i]
        if code == 0 or i + code > n:
            raise FrameError("bad COBS code")
        out += data[i + 1:i + code]
        i += code
        if code != 0xFF and i < n:
            out.append(0)
    return bytes(out)


def encode(f: Frame) -> bytes:
    if len(f.payload) > MAX_PAYLOAD:
        raise FrameError("payload too large")
    body = struct.pack("<BBH", f.type, f.flags, f.chan) + f.payload
    body += struct.pack("<I", zlib.crc32(body))
    return cobs_encode(body) + b"\x00"


def decode(raw: bytes) -> Frame:
    """`raw` is one frame without its trailing 0x00."""
    body = cobs_decode(raw)
    if len(body) < 8:
        raise FrameError("short frame")
    if len(body) > MAX_BODY:
        raise FrameError("frame too long")
    (crc,) = struct.unpack("<I", body[-4:])
    if zlib.crc32(body[:-4]) != crc:
        raise FrameError("crc mismatch")
    t, flags, chan = struct.unpack("<BBH", body[:4])
    return Frame(t, chan, bytes(body[4:-4]), flags)


class Deframer:
    """Feed arbitrary byte chunks from the serial port; yields complete frames.

    Corrupt or oversized frames are counted in `errors` and dropped, never raised: the stream
    resyncs at the next 0x00. Because a dropped DATA frame corrupts the byte stream above it,
    the owner must treat any growth of `errors` while a session is RUNNING as a link fault and
    reset all channels (see ARCHITECTURE.md, "Link faults")."""

    def __init__(self):
        self._buf = bytearray()
        self._overflow = False
        self.errors = 0

    def feed(self, chunk: bytes):
        for b in chunk:
            if b:
                if len(self._buf) >= MAX_RAW:
                    self._overflow = True
                else:
                    self._buf.append(b)
                continue
            if self._overflow:
                self.errors += 1
            elif self._buf:
                try:
                    yield decode(bytes(self._buf))
                except FrameError:
                    self.errors += 1
            self._buf.clear()
            self._overflow = False


# ---- payload helpers ---------------------------------------------------------------------

_HELLO = struct.Struct("<BBBHBI")


def hello(role: Role, state: NodeState, session: int, version: str,
          max_payload: int = MAX_PAYLOAD, max_channels: int = MAX_CHANNELS) -> Frame:
    """HELLO payload: proto u8 | role u8 | state u8 | max_payload u16 | max_channels u8 | session u32 | version utf8"""
    return Frame(T.HELLO, CONTROL_CHAN,
                 _HELLO.pack(PROTO_VERSION, role, state, max_payload, max_channels, session)
                 + version.encode()[:32])


def parse_hello(p: bytes) -> dict:
    if len(p) < _HELLO.size:
        raise FrameError("short HELLO")
    proto, role, state, max_payload, max_channels, session = _HELLO.unpack_from(p)
    return dict(proto=proto, role=Role(role), state=NodeState(state), max_payload=max_payload,
                max_channels=max_channels, session=session,
                version=p[_HELLO.size:].decode(errors="replace"))


def accept(chan: int, listener: int, ip: bytes, port: int) -> Frame:
    if len(ip) != 4:
        raise FrameError("ip4 expected")
    return Frame(T.ACCEPT, chan, struct.pack("<H4sH", listener, ip, port))


def parse_accept(p: bytes) -> tuple:
    if len(p) != 8:
        raise FrameError("bad ACCEPT")
    listener, ip, port = struct.unpack("<H4sH", p)
    return listener, ip, port


def credit(chan: int, n: int) -> Frame:
    return Frame(T.CREDIT, chan, struct.pack("<I", n))


def parse_credit(p: bytes) -> int:
    if len(p) != 4:
        raise FrameError("bad CREDIT")
    return struct.unpack("<I", p)[0]


def close(chan: int, reason: Close = Close.EOF) -> Frame:
    return Frame(T.CLOSE, chan, bytes([reason]))


def parse_close(p: bytes) -> Close:
    if len(p) != 1:
        raise FrameError("bad CLOSE")
    return Close(p[0])


def data_frames(chan: int, data: bytes):
    """Split a byte string into DATA frames of at most MAX_PAYLOAD."""
    for i in range(0, len(data), MAX_PAYLOAD):
        yield Frame(T.DATA, chan, data[i:i + MAX_PAYLOAD])


def config_frames(blob: bytes):
    """Split a CONFIG JSON blob into chunks; all but the last carry FLAG_MORE."""
    if not 0 < len(blob) <= MAX_CONFIG:
        raise FrameError("config size out of range")
    chunks = [blob[i:i + MAX_PAYLOAD] for i in range(0, len(blob), MAX_PAYLOAD)]
    for n, c in enumerate(chunks):
        yield Frame(T.CONFIG, CONTROL_CHAN, c, FLAG_MORE if n < len(chunks) - 1 else 0)


class ConfigAssembler:
    """Reassembles chunked CONFIG frames. Returns the full blob on the last chunk, else None."""

    def __init__(self):
        self._buf = bytearray()

    def add(self, f: Frame):
        self._buf += f.payload
        if len(self._buf) > MAX_CONFIG:
            self._buf.clear()
            raise FrameError("config too large")
        if f.flags & FLAG_MORE:
            return None
        blob = bytes(self._buf)
        self._buf.clear()
        return blob
