"""One proxied connection between a node channel and a TCP stream on the PC side.

Channel: the node accepted a client on its Wi-Fi side; we connect to the listener's target (inbound).
OutboundChannel: a local client connected to a forwarded port; the node connects to the endpoint (outbound)."""
import asyncio
import logging
import time

from . import protocol as P

log = logging.getLogger("tether.bridge")

LINGER = 60.0   # seconds a half-closed channel may wait for the other direction (ARCHITECTURE.md 4.6)

RESP_502 = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
RESP_504 = b"HTTP/1.1 504 Gateway Timeout\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
RESP_503 = b"HTTP/1.1 503 Service Unavailable\r\nRetry-After: 2\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


class Channel:
    def __init__(self, session, chan, listener, peer, window, connect_timeout, http=None):
        self.s = session
        self.id = chan
        self.listener = listener
        self.http = listener.http if http is None else http
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
        self._w_state = 0                 # writer task: 0 not started, 1 running (data may be queued), 2 finished
        self.done = False
        self.bytes_up = 0                 # node -> target
        self.bytes_down = 0               # target -> node
        self.t0 = time.monotonic()
        self._reader = self._writer = None
        self._linger = None
        self._tasks = [asyncio.ensure_future(self._run())]

    UP, DOWN = "node->target", "target->node"

    def describe(self):
        l = self.listener
        return f"listener {l.id} from {self.peer} -> {l.target_host}:{l.target_port}"

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
        elif reason == P.Close.NO_CHANNEL and self.eof_sent and self.eof_recv:
            pass   # we sent a CREDIT for a channel the node had just finished: benign, keep draining our queue
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
            self._start_writer()
            await self._to_node()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("channel %d failed", self.id)
            self.s.send(P.close(self.id, P.Close.RESET))
            self.abort()

    async def _fail(self, response):
        if self.http:
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

    def _start_writer(self):
        self._w_state = 1
        self._tasks.append(asyncio.ensure_future(self._writer_task()))

    async def _writer_task(self):
        try:
            while True:
                item = await self._wq.get()
                if item is None:
                    if self._writer.can_write_eof():
                        self._writer.write_eof()
                    self._w_state = 2
                    self._maybe_finish()
                    return
                self._writer.write(item)
                await self._writer.drain()
                self._consumed += len(item)
                if self._consumed >= self.window // 2 and not self.eof_recv:   # the node sends no more data after its EOF
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
        if self.eof_sent and self.eof_recv and self._w_state != 1:   # wait until the queued data reached the stream
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
        log.info("connection closed: %s, %d B %s, %d B %s, %.1f s", self.describe(),
                 self.bytes_up, self.UP, self.bytes_down, self.DOWN, time.monotonic() - self.t0)


class OutboundChannel(Channel):
    """A local client connected to a forwarded port; the node connects to the configured endpoint."""
    UP, DOWN = "node->client", "client->node"

    def __init__(self, session, chan, forward, peer, window, reader, writer):
        self.forward = forward
        self._pre = (reader, writer)
        super().__init__(session, chan, None, peer, window, forward.connect_timeout, http=forward.http)

    def describe(self):
        f = self.forward
        return f"forward {f.id} from {self.peer} -> {f.target_host}:{f.target_port}"

    async def _run(self):
        self._reader, self._writer = self._pre
        f, s = self.forward, self.s
        fut = s.expect_open(self.id)
        s.send(P.open_req(self.id, f.id))
        try:
            try:
                status = await asyncio.wait_for(fut, f.connect_timeout)
            except asyncio.TimeoutError:
                log.warning("forward %d: the node did not connect to %s:%d in time", f.id, f.target_host, f.target_port)
                s.send(P.close(self.id, P.Close.RESET))   # frees the node's channel if it is still connecting
                await self._refuse(RESP_504)
                return
            if status != P.Open.OK:
                log.warning("forward %d: %s:%d -> %s", f.id, f.target_host, f.target_port, status.name)
                await self._refuse(RESP_503 if status in (P.Open.NO_CAPACITY, P.Open.NOT_READY) else RESP_502)
                return
            self._start_writer()
            await self._to_node()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("channel %d failed", self.id)
            s.send(P.close(self.id, P.Close.RESET))
            self.abort()

    async def _refuse(self, response):
        """The node has no channel for us: answer the local client directly and end."""
        try:
            if self.http:
                self._writer.write(response)
                await asyncio.wait_for(self._writer.drain(), 2)
        except (OSError, asyncio.TimeoutError):
            pass
        self.abort()
