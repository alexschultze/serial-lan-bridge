"""Wire protocol between the bridge PC and the ESP32-S3.

Frame on the wire:  COBS( type u8 | flags u8 | chan u16le | payload | crc32 u32le ) 0x00
The CRC covers everything before it. 0x00 never occurs inside a frame, so a
receiver that loses sync recovers at the next delimiter.
"""
import struct
import zlib
from dataclasses import dataclass
from enum import IntEnum

MAX_PAYLOAD = 1024  # keeps ESP32 buffers small; DATA frames are split to this


class T(IntEnum):
    HELLO = 0x01
    PING = 0x02
    PONG = 0x03
    WIFI_SET = 0x10      # payload: ssid\0password
    WIFI_STATUS = 0x11   # payload: state u8 | ip4 | gw4 | rssi i8
    OPEN_TCP = 0x20      # chan = new channel id; payload: port u16 | host (utf8, name or ip)
    OPEN_UDP = 0x21
    LISTEN = 0x22        # chan = listener id; payload: port u16 (ESP32 listens on its Wi-Fi network)
    OPEN_RES = 0x23      # payload: status u8 (0 = ok) | initial credit u32
    ACCEPT = 0x24        # chan = new inbound channel; payload: listener id u16 | peer ip4 | peer port u16
    DATA = 0x30
    CREDIT = 0x31        # payload: bytes u32 the sender may send additionally
    CLOSE = 0x32
    RESOLVE = 0x40
    RESOLVE_RES = 0x41
    DGRAM = 0x42         # UDP: payload ip4 | port u16 | data


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
    if len(f.payload) > MAX_PAYLOAD + 16:
        raise FrameError("payload too large")
    body = struct.pack("<BBH", f.type, f.flags, f.chan) + f.payload
    body += struct.pack("<I", zlib.crc32(body))
    return cobs_encode(body) + b"\x00"


def decode(raw: bytes) -> Frame:
    """`raw` is one frame without its trailing 0x00."""
    body = cobs_decode(raw)
    if len(body) < 8:
        raise FrameError("short frame")
    (crc,) = struct.unpack("<I", body[-4:])
    if zlib.crc32(body[:-4]) != crc:
        raise FrameError("crc mismatch")
    t, flags, chan = struct.unpack("<BBH", body[:4])
    return Frame(t, chan, bytes(body[4:-4]), flags)


class Deframer:
    """Feed arbitrary byte chunks from the serial port; yields complete frames.
    Corrupt frames are counted and dropped, never raised: the stream resyncs
    at the next 0x00."""

    def __init__(self):
        self._buf = bytearray()
        self.errors = 0

    def feed(self, chunk: bytes):
        for b in chunk:
            if b:
                self._buf.append(b)
                continue
            if self._buf:
                try:
                    yield decode(bytes(self._buf))
                except FrameError:
                    self.errors += 1
                self._buf.clear()
