"""One proxied connection between a node channel and a TCP stream on the PC side.

Channel: the node accepted a client on its Wi-Fi side; we connect to the listener's target (inbound).
OutboundChannel: a local client connected to a forwarded port; the node connects to the endpoint (outbound)."""
import asyncio
import logging
import time

from . import protocol as P
from .httprewrite import HostRewriter
from .tlsutil import TlsClient, client_context
import ssl
from . import universal as U
from .httpresponse import ResponseRewriter, make_mapper

log = logging.getLogger("tether.bridge")

LINGER = 60.0   # seconds a half-closed channel may wait for the other direction (ARCHITECTURE.md 4.6)

RESP_502 = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
RESP_504 = b"HTTP/1.1 504 Gateway Timeout\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_BODY_400 = b"use /<http|https>/<host>[:port]/<path> as the address\n"
RESP_400 = (b"HTTP/1.1 400 Bad Request\r\nContent-Type: text/plain\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
            % len(_BODY_400)) + _BODY_400
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
        self._dest = None                 # "host:port" of a universal channel once its first request has named it
        hv = listener.host_value if listener is not None else None   # inbound: requests on their way to the target
        self._target_rw = HostRewriter(hv) if hv else None
        self._resp_rw = None              # server -> client: redirects are mapped back onto the bridge
        if self._target_rw is not None and listener is not None:
            self._resp_rw = ResponseRewriter(self._target_rw, make_mapper(session.cfg, "listener", listener))
        self._linger = None
        self._tasks = [asyncio.ensure_future(self._run())]

    UP, DOWN = "node->target", "target->node"

    def describe(self):
        l = self.listener
        return f"listener {l.id} from {self.peer} -> {self._dest or f'{l.target_host}:{l.target_port}'}"

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
        log.debug("channel 0x%04x: node sent CLOSE %s", self.id, reason.name)
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
            host, port, use_tls = lst.target_host, lst.target_port, bool(lst.tls)
            if lst.universal:   # the first request names the destination: /<http|https>/<host>[:port]/<path>
                tg = await self._pick_target()
                if tg is None:
                    return
                host, port, use_tls = tg.host, tg.port, tg.tls
                self._target_rw = HostRewriter(tg.host_value, tg.prefix)
                self._resp_rw = ResponseRewriter(
                    self._target_rw, make_mapper(self.s.cfg, "listener", lst, (tg.scheme, tg.host, tg.port)))
                self._dest = f"{tg.scheme}://{tg.host}:{tg.port}"
            log.debug("channel %d: connecting to %s:%d", self.id, host, port)
            try:
                ctx = client_context(lst.tls_verify) if use_tls else None   # HTTPS on the leg to the target
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port, ssl=ctx, server_hostname=host if ctx else None), self.connect_timeout)
            except asyncio.TimeoutError:
                log.warning("channel %d: connect to %s:%d timed out", self.id, host, port)
                await self._fail(RESP_504)
                return
            except OSError as e:
                log.warning("channel %d: connect to %s:%d failed: %s", self.id, host, port, e)
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

    async def _pick_target(self):
        """Universal listener: waits for the first request head from the node and reads the destination out of its path.
        The collected bytes go back to the front of the queue, so the writer sends them on (rewritten) like any others."""
        buf, ended = b"", False
        try:
            while b"\r\n\r\n" not in buf and len(buf) <= 65536:
                item = await asyncio.wait_for(self._wq.get(), 15)
                if item is None:
                    ended = True
                    break
                buf += item
        except asyncio.TimeoutError:
            pass
        tg = None if ended else U.parse(U.request_target(buf) or b"")
        if tg is None:
            if buf and not ended:
                log.warning("channel %d: %s sent no /<http|https>/<host>/ address; answering 400", self.id, self.peer)
            if buf and not ended:
                await self._fail(RESP_400)
            else:
                self.abort()   # the client left, or sent nothing in time
            return None
        q = asyncio.Queue()
        q.put_nowait(buf)
        while not self._wq.empty():
            q.put_nowait(self._wq.get_nowait())
        self._wq = q
        return tg

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
            if self._resp_rw is not None:   # responses of the target, with redirects mapped onto the bridge
                await self._send_out(self._resp_rw.feed(data) if data else self._resp_rw.flush())
                if not data:
                    self._send_eof()
                    return
                continue
            if not data:
                self._send_eof()
                return
            self.credit_out -= len(data)
            self.bytes_down += len(data)
            self.s.send(P.Frame(P.T.DATA, self.id, data))

    def _eof_tail(self):
        return b"" if self._target_rw is None else self._target_rw.flush()

    async def _from_node(self, item):
        """Bytes from the node on their way to the stream: returns (bytes to write, the stream ended)."""
        if self._target_rw is None:
            return item, False
        out = self._target_rw.feed(item)
        if self._target_rw.bad:   # a later request on this connection names another destination
            log.warning("channel %d: a request changed the destination on a kept-alive connection; resetting", self.id)
            self.s.send(P.close(self.id, P.Close.RESET))
            raise ConnectionError("destination changed")
        return out, False

    async def _send_out(self, out):
        """Sends bytes to the node within the credit it granted; a rewritten or encrypted chunk can exceed what was read."""
        while out:
            while self.credit_out <= 0:
                self._credit_ev.clear()
                await self._credit_ev.wait()
            n = min(len(out), self.credit_out, P.MAX_PAYLOAD)
            self.credit_out -= n
            self.bytes_down += n
            self.s.send(P.Frame(P.T.DATA, self.id, out[:n]))
            out = out[n:]

    def _start_writer(self):
        self._w_state = 1
        self._tasks.append(asyncio.ensure_future(self._writer_task()))

    async def _writer_task(self):
        try:
            while True:
                item = await self._wq.get()
                if item is None:
                    out, eof = b"", True
                else:
                    out, eof = await self._from_node(item)
                if out:
                    self._writer.write(out)
                    await self._writer.drain()
                if item is not None:
                    self._consumed += len(item)   # credit is for the bytes the node sent, whatever was made of them
                    if self._consumed >= self.window // 2 and not self.eof_recv:   # the node sends no more data after its EOF
                        self.s.send(P.credit(self.id, self._consumed))
                        self._rx_outstanding += self._consumed
                        self._consumed = 0
                if eof:
                    rest = self._eof_tail()   # an incomplete head that was held back goes on unchanged
                    if rest:
                        self._writer.write(rest)
                    if self._writer.can_write_eof():
                        self._writer.write_eof()
                    else:
                        # a TLS stream cannot be half-closed: without this the site keeps its connection (and our channel)
                        # open until the 60 s linger runs out, and 16 such channels block every new connection
                        self._writer.close()
                    self._w_state = 2
                    self._maybe_finish()
                    return
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
        hv = forward.host_value
        self._rewriter = HostRewriter(hv) if hv else None   # client -> server: requests get a new Host header
        # HTTPS on the leg to the server: the bytes pass through the node, so TLS runs here over in-memory buffers
        self._tls = TlsClient(client_context(forward.tls_verify), forward.target_host) if forward.tls else None
        self._initial = b""   # universal: the first request head, read before the destination was known
        super().__init__(session, chan, None, peer, window, forward.connect_timeout, http=forward.http)
        self._resp_rw = None   # server -> client: redirects are mapped back onto the bridge
        if self._rewriter is not None:
            self._resp_rw = ResponseRewriter(self._rewriter, make_mapper(session.cfg, "forward", forward))

    def _down(self, data):
        return data if self._resp_rw is None else self._resp_rw.feed(data)

    def _eof_tail(self):
        return b"" if self._resp_rw is None else self._resp_rw.flush()

    async def _to_node(self):
        if self._rewriter is None and self._tls is None:
            await super()._to_node()
            return
        while not self.done:
            while self.credit_out <= 0:
                self._credit_ev.clear()
                await self._credit_ev.wait()
            data = await self._reader.read(min(P.MAX_PAYLOAD, self.credit_out))
            if data:
                out = data if self._rewriter is None else self._rewriter.feed(data)
                if self._rewriter is not None and self._rewriter.bad:   # a later request names another destination
                    log.warning("channel %d: a request changed the destination on a kept-alive connection; resetting", self.id)
                    self.s.send(P.close(self.id, P.Close.RESET))
                    self.abort()
                    return
                if self._tls is not None:
                    out = self._tls.encrypt(out)
            else:   # the client is done: release a held-back request head, then say goodbye to the server
                out = b"" if self._rewriter is None else self._rewriter.flush()
                if self._tls is not None:
                    out = self._tls.encrypt(out) + self._tls.close()
            await self._send_out(out)
            if not data:
                self._send_eof()
                return

    async def _from_node(self, item):
        if self._tls is None:
            return self._down(item), False
        try:
            plain, out, eof = self._tls.feed(item)
        except ssl.SSLError as e:
            f = self.forward
            log.warning("forward %d: TLS to %s:%d failed: %s", f.id, f.target_host, f.target_port,
                        getattr(e, "verify_message", None) or e)
            try:
                if self.http:
                    self._writer.write(RESP_502)
                    await asyncio.wait_for(self._writer.drain(), 2)
            except (OSError, asyncio.TimeoutError):
                pass
            self.s.send(P.close(self.id, P.Close.RESET))
            raise ConnectionError("TLS failed") from e
        if out:
            await self._send_out(out)
        return self._down(plain), eof

    def describe(self):
        f = self.forward
        return f"forward {f.id} from {self.peer} -> {self._dest or f.label}"

    async def _run(self):
        self._reader, self._writer = self._pre
        f, s = self.forward, self.s
        host, port = f.target_host, f.target_port
        open_args = ()
        if f.universal:   # the first request names the destination: /<http|https>/<host>[:port]/<path>
            head = await self._client_head()
            tg = U.parse(U.request_target(head) or b"") if head else None
            if tg is None:
                if head:
                    log.warning("forward %d: %s sent no /<http|https>/<host>/ address; answering 400", f.id, self.peer)
                    await self._refuse(RESP_400)
                else:
                    self.abort()   # the client left, or sent nothing in time
                return
            host, port, open_args = tg.host, tg.port, (tg.host, tg.port)
            self._dest = f"{tg.scheme}://{tg.host}:{tg.port}"
            self._rewriter = HostRewriter(tg.host_value, tg.prefix)
            self._resp_rw = ResponseRewriter(
                self._rewriter, make_mapper(s.cfg, "forward", f, (tg.scheme, tg.host, tg.port)))
            self._tls = TlsClient(client_context(f.tls_verify), tg.host) if tg.tls else None
            self._initial = head
        log.debug("channel 0x%04x: OPEN forward %d (%s:%d)", self.id, f.id, host, port)
        fut = s.expect_open(self.id)
        s.send(P.open_req(self.id, f.id, *open_args))
        try:
            try:
                status = await asyncio.wait_for(fut, f.connect_timeout)
            except asyncio.TimeoutError:
                log.warning("forward %d: the node did not connect to %s:%d in time", f.id, host, port)
                s.send(P.close(self.id, P.Close.RESET))   # frees the node's channel if it is still connecting
                await self._refuse(RESP_504)
                return
            if status != P.Open.OK:
                log.warning("forward %d: %s:%d -> %s", f.id, host, port, status.name)
                await self._refuse(RESP_503 if status in (P.Open.NO_CAPACITY, P.Open.NOT_READY) else RESP_502)
                return
            self._start_writer()
            if self._tls is not None:
                await self._send_out(self._tls.start())   # ClientHello
            if self._initial:   # universal: the request head that named the destination goes first, rewritten
                out = self._rewriter.feed(self._initial)
                await self._send_out(self._tls.encrypt(out) if self._tls is not None else out)
            await self._to_node()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("channel %d failed", self.id)
            s.send(P.close(self.id, P.Close.RESET))
            self.abort()

    async def _client_head(self):
        """Reads the first request head from the local client (up to 64 KB, 15 s); None if it never completes."""
        buf = b""
        while b"\r\n\r\n" not in buf:
            if len(buf) > 65536:
                break
            try:
                data = await asyncio.wait_for(self._reader.read(65536), 15)
            except asyncio.TimeoutError:
                return None
            if not data:
                return None
            buf += data
        return buf

    async def _refuse(self, response):
        """The node has no channel for us: answer the local client directly and end."""
        try:
            if self.http:
                self._writer.write(response)
                await asyncio.wait_for(self._writer.drain(), 2)
        except (OSError, asyncio.TimeoutError):
            pass
        self.abort()
