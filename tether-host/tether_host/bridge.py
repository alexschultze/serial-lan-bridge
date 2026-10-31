"""One proxied connection: node channel <-> TCP connection to the listener's target."""
import asyncio
import logging
import time

from . import protocol as P

log = logging.getLogger("tether.bridge")

LINGER = 60.0   # seconds a half-closed channel may wait for the other direction (ARCHITECTURE.md 4.6)

RESP_502 = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
RESP_504 = b"HTTP/1.1 504 Gateway Timeout\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


class Channel:
    def __init__(self, session, chan, listener, peer, window, connect_timeout):
        self.s = session
        self.id = chan
        self.listener = listener
        self.peer = peer
        self.window = window
        self.connect_timeout = connect_timeout
        self.credit_out = window          # bytes we may still send to the node
        self._credit_ev = asyncio.Event()
        self._rx_outstanding = window     # bytes the node may still send us
        self._consumed = 0                # bytes written to the target since the last CREDIT
        self._wq = asyncio.Queue()
        self.eof_sent = False
        self.eof_recv = False
        self.done = False
        self.bytes_up = 0                 # node -> target
        self.bytes_down = 0               # target -> node
        self.t0 = time.monotonic()
        self._reader = self._writer = None
        self._linger = None
        self._tasks = [asyncio.ensure_future(self._run())]

    # ---- frames arriving from the node (called by the session dispatcher) ------------------
    def on_data(self, data):
        self._rx_outstanding -= len(data)
        if self._rx_outstanding < 0:
            log.warning("channel %d: credit overrun", self.id)
            self.s.send(P.close(self.id, P.Close.PROTO))
            self.abort()
            return
        self.bytes_up += len(data)
        self._wq.put_nowait(data)

    def on_credit(self, n):
        self.credit_out += n
        self._credit_ev.set()

    def on_close(self, reason):
        if reason == P.Close.EOF:
            self.eof_recv = True
            self._wq.put_nowait(None)
            self._start_linger()
            self._maybe_finish()
        else:
            self.abort()

    # ---- internals --------------------------------------------------------------------------
    async def _run(self):
        lst = self.listener
        try:
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(lst.target_host, lst.target_port), self.connect_timeout)
            except asyncio.TimeoutError:
                log.warning("channel %d: connect to %s:%d timed out", self.id, lst.target_host, lst.target_port)
                await self._fail(RESP_504)
                return
            except OSError as e:
                log.warning("channel %d: connect to %s:%d failed: %s", self.id, lst.target_host, lst.target_port, e)
                await self._fail(RESP_502)
                return
            self._tasks.append(asyncio.ensure_future(self._writer_task()))
            await self._to_node()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("channel %d failed", self.id)
            self.s.send(P.close(self.id, P.Close.RESET))
            self.abort()

    async def _fail(self, response):
        if self.listener.http:
            for f in P.data_frames(self.id, response):
                self.s.send(f)
            self._send_eof()
            self._wq.put_nowait(None)   # discard whatever the client sends; the writer is not running
            self._start_linger()
        else:
            self.s.send(P.close(self.id, P.Close.RESET))
            self.abort()

    async def _to_node(self):
        while not self.done:
            while self.credit_out <= 0:
                self._credit_ev.clear()
                await self._credit_ev.wait()
            data = await self._reader.read(min(P.MAX_PAYLOAD, self.credit_out))
            if not data:
                self._send_eof()
                return
            self.credit_out -= len(data)
            self.bytes_down += len(data)
            self.s.send(P.Frame(P.T.DATA, self.id, data))

    async def _writer_task(self):
        try:
            while True:
                item = await self._wq.get()
                if item is None:
                    if self._writer.can_write_eof():
                        self._writer.write_eof()
                    return
                self._writer.write(item)
                await self._writer.drain()
                self._consumed += len(item)
                if self._consumed >= self.window // 2:
                    self.s.send(P.credit(self.id, self._consumed))
                    self._rx_outstanding += self._consumed
                    self._consumed = 0
        except (ConnectionError, OSError):
            self.s.send(P.close(self.id, P.Close.RESET))
            self.abort()

    def _send_eof(self):
        if not self.eof_sent:
            self.eof_sent = True
            self.s.send(P.close(self.id, P.Close.EOF))
            self._start_linger()
            self._maybe_finish()

    def _start_linger(self):
        if self._linger is None and not self.done:
            self._linger = asyncio.get_running_loop().call_later(LINGER, self._linger_expired)

    def _linger_expired(self):
        if not self.done:
            log.info("channel %d: half-closed too long, resetting", self.id)
            self.s.send(P.close(self.id, P.Close.RESET))
            self.abort()

    def _maybe_finish(self):
        if self.eof_sent and self.eof_recv:
            self._finish()

    def abort(self):
        if self._writer is not None:
            tr = self._writer.transport
            try:
                tr.abort()
            except Exception:
                pass
        self._finish()

    def _finish(self):
        if self.done:
            return
        self.done = True
        if self._linger:
            self._linger.cancel()
        me = asyncio.current_task()
        for t in self._tasks:
            if t is not me and not t.done():
                t.cancel()
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
        self.s.channel_done(self)
        log.info("connection closed: listener %d from %s -> %s:%d, %d B up, %d B down, %.1f s",
                 self.listener.id, self.peer, self.listener.target_host, self.listener.target_port,
                 self.bytes_up, self.bytes_down, time.monotonic() - self.t0)
