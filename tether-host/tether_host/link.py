"""Byte transports to the node: a pyserial implementation and an in-memory one for tests."""
import asyncio
import logging
import queue
import sys
import threading

log = logging.getLogger("tether.link")

# USB identity of tether-node: pid.codes vendor ID with a test product ID (fine for own use; register a PID before
# distributing). Must match CONFIG_TINYUSB_DESC_CUSTOM_VID/PID in tether-node/sdkconfig.defaults.
TETHER_VID = 0x1209
TETHER_PID = 0x0001
ESPRESSIF_VID = 0x303A
ROM_BOOTLOADER_PID = 0x1001   # the chip's ROM USB-Serial/JTAG: a board in download mode or without tether firmware


class LinkClosed(Exception):
    pass


class Transport:
    """Async read of raw byte chunks, non-blocking write. Both raise LinkClosed once the link is gone."""

    async def read(self) -> bytes:
        raise NotImplementedError

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class LoopbackTransport(Transport):
    def __init__(self):
        self._q = asyncio.Queue()
        self.peer = None
        self.closed = False

    async def read(self):
        item = await self._q.get()
        if item is None:
            raise LinkClosed("peer closed")
        return item

    def write(self, data):
        if self.closed or self.peer is None or self.peer.closed:
            raise LinkClosed("closed")
        self.peer._q.put_nowait(bytes(data))

    async def close(self):
        if not self.closed:
            self.closed = True
            self._q.put_nowait(None)
            if self.peer and not self.peer.closed:
                self.peer._q.put_nowait(None)


def loopback_pair():
    a, b = LoopbackTransport(), LoopbackTransport()
    a.peer, b.peer = b, a
    return a, b


_warned = set()   # ports already reported as being in download mode (the search repeats on every reconnect attempt)


def find_ports(selector="auto", serial_number=""):
    """Return the serial ports that look like a tether-node (or the explicit one)."""
    from serial.tools import list_ports
    if selector and selector != "auto":
        return [selector]
    out = []
    for p in list_ports.comports():
        ident = f"{p.device} ({p.vid or 0:04X}:{p.pid or 0:04X} {p.product or p.description or ''})"
        if p.vid == ESPRESSIF_VID and p.pid == ROM_BOOTLOADER_PID:
            (log.debug if p.device in _warned else log.warning)("port %s is an ESP32 in ROM/download mode or without tether firmware: flash tether-node, "
                        "then reset the board", ident)
            _warned.add(p.device)
            continue
        # Windows' port list usually lacks the USB product string, so the VID:PID is what identifies the node there
        if (p.vid, p.pid) != (TETHER_VID, TETHER_PID) and "tether" not in (p.product or "").lower():
            log.debug("port %s: not a tether-node", ident)
            continue
        if serial_number and (p.serial_number or "") != serial_number:
            log.debug("port %s: serial number %s does not match host.serial", ident, p.serial_number)
            continue
        log.debug("port %s: tether-node, serial %s", ident, p.serial_number)
        out.append(p.device)
    return out


class SerialTransport(Transport):
    def __init__(self, port):
        import serial
        self._ser = serial.Serial(port, 115200, timeout=0.05, write_timeout=5)  # baud is ignored by CDC
        if sys.platform == "win32":
            try:
                self._ser.set_buffer_size(rx_size=65536, tx_size=65536)
            except Exception:
                pass
        self._ser.reset_input_buffer()
        log.debug("opened %s (DTR=%s RTS=%s)", port, self._ser.dtr, self._ser.rts)
        self._loop = asyncio.get_running_loop()
        self._rx = asyncio.Queue()
        self._tx = queue.Queue()
        self.closed = False
        self.port = port
        self._threads = [threading.Thread(target=self._reader, daemon=True, name="serial-rx"),
                         threading.Thread(target=self._writer, daemon=True, name="serial-tx")]
        for t in self._threads:
            t.start()

    def _post(self, item):
        try:
            self._loop.call_soon_threadsafe(self._rx.put_nowait, item)
        except RuntimeError:   # event loop already closed
            pass

    def _reader(self):
        try:
            while not self.closed:
                data = self._ser.read(self._ser.in_waiting or 1)
                if data:
                    self._post(data)
        except Exception as e:
            if not self.closed:
                log.warning("serial read failed: %s (device unplugged or reset?)", e)
        finally:
            self.closed = True
            self._post(None)

    def _writer(self):
        try:
            while True:
                data = self._tx.get()
                if data is None or self.closed:
                    break
                self._ser.write(data)
        except Exception as e:
            if not self.closed:
                log.warning("serial write failed: %s (the node stopped reading?)", e)
        finally:
            self.closed = True
            self._post(None)

    async def read(self):
        item = await self._rx.get()
        if item is None:
            self._rx.put_nowait(None)
            raise LinkClosed("serial port closed")
        return item

    def write(self, data):
        if self.closed:
            raise LinkClosed("serial port closed")
        self._tx.put(bytes(data))

    async def close(self):
        self.closed = True
        self._tx.put(None)
        try:
            self._ser.close()
        except Exception:
            pass
        for t in self._threads:
            await asyncio.get_running_loop().run_in_executor(None, t.join, 1.0)
        self._post(None)
