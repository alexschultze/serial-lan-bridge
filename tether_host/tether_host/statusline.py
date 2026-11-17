"""A status line pinned to the bottom row of the console, refreshed once per second.

The last terminal row is taken out of the scrolling region, so log lines scroll above it and the line itself never moves.
It is only used when stderr is an interactive terminal that understands ANSI escape sequences."""
import asyncio
import os
import shutil
import sys

from . import protocol as P

_ESC = "\x1b"


def _enable_vt():
    """Turns on ANSI escape handling of the Windows console (Windows Terminal has it on already). False if unavailable."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-12)   # STD_ERROR_HANDLE
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        return bool(k.SetConsoleMode(h, mode.value | 0x0004))   # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        return False


def usable(stream=None):
    stream = stream or sys.stderr
    try:
        return stream.isatty() and os.environ.get("TERM") != "dumb" and _enable_vt()
    except Exception:
        return False


def rate(bps):
    if bps >= 1024 * 1024:
        return "%.1f MB/s" % (bps / 1048576)
    if bps >= 1024:
        return "%.1f KB/s" % (bps / 1024)
    return "%d B/s" % bps


def size(n):
    for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return "%.1f %s" % (n / div, unit)
    return "%d B" % n


def render(session, rx_rate, tx_rate):
    """The text of the status line (no escape sequences)."""
    up = session.link_up
    node = session.node_status or {}
    parts = ["link " + ("up" if up else "DOWN, searching for the node")]
    if up:
        state = node.get("state") or "?"
        parts.append("node " + state)
        if node.get("ip"):
            parts.append("%s%s" % (node["ip"], " %s dBm" % node["rssi"] if node.get("rssi") else ""))
    parts.append("connections %d/%d" % (len(session.channels), P.MAX_CHANNELS))
    parts.append("from node %s  to node %s" % (rate(rx_rate), rate(tx_rate)))
    parts.append("total from node %s  to node %s" % (size(session.rx_bytes), size(session.tx_bytes)))
    return " | ".join(parts)


class StatusLine:
    def __init__(self, session, stream=None):
        self.session = session
        self.out = stream or sys.stderr
        self._rows = 0
        self._last = (session.rx_bytes, session.tx_bytes)

    def _write(self, s):
        try:
            self.out.write(s)
            self.out.flush()
        except (OSError, ValueError):
            pass

    def _layout(self):
        """Reserves the last row (again, after a resize)."""
        cols, rows = shutil.get_terminal_size((100, 30))
        if rows != self._rows:
            self._rows = rows
            self._write("%s7%s[1;%dr%s8" % (_ESC, _ESC, max(rows - 1, 1), _ESC))   # save cursor, scroll region, restore
        return cols, rows

    def draw(self, rx_rate, tx_rate):
        cols, rows = self._layout()
        text = render(self.session, rx_rate, tx_rate)[:max(cols - 1, 1)].ljust(max(cols - 1, 1))
        # save cursor, jump to the last row, reverse video, text, back to normal, restore cursor
        self._write("%s7%s[%d;1H%s[7m%s%s[0m%s8" % (_ESC, _ESC, rows, _ESC, text, _ESC, _ESC))

    def close(self):
        if self._rows:
            self._write("%s[r%s[%d;1H%s[2K" % (_ESC, _ESC, self._rows, _ESC))   # whole screen scrolls again, line cleared
            self._rows = 0

    async def run(self):
        try:
            self.draw(0, 0)
            while True:
                await asyncio.sleep(1.0)
                rx, tx = self.session.rx_bytes, self.session.tx_bytes
                self.draw(rx - self._last[0], tx - self._last[1])
                self._last = (rx, tx)
        finally:
            self.close()
