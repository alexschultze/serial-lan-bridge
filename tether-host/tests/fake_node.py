"""A node implemented in Python over a Transport, for hardware-free tests of tether-host.

Follows ARCHITECTURE.md section 4/5 but binds listeners on 127.0.0.1 and ignores Wi-Fi."""
import asyncio
import json
import os
import socket
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tether_host import protocol as P  # noqa: E402
from tether_host.link import LinkClosed  # noqa: E402

RESP_503 = b"HTTP/1.1 503 Service Unavailable\r\nRetry-After: 2\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


class FChan:
    def __init__(self, node, cid, reader, writer, window):
        self.n, self.id, self.r, self.w, self.window = node, cid, reader, writer, window
        self.credit_out = window
        self.ev = asyncio.Event()
        self.rx_outstanding = window
        self.consumed = 0
        self.q = asyncio.Queue()
        self.eof_sent = self.eof_recv = False
        self.w_done = False
        self.tasks = [asyncio.ensure_future(self.to_host()), asyncio.ensure_future(self.from_host())]

    async def to_host(self):
        try:
            while True:
                while self.credit_out <= 0:
                    self.ev.clear()
                    await self.ev.wait()
                data = await self.r.read(min(P.MAX_PAYLOAD, self.credit_out))
                if not data:
                    self.eof_sent = True
                    self.n.send(P.close(self.id, P.Close.EOF))
                    self.maybe_done()
                    return
                self.credit_out -= len(data)
                self.n.send(P.Frame(P.T.DATA, self.id, data))
        except (ConnectionError, OSError):
            self.n.send(P.close(self.id, P.Close.RESET))
            self.drop()

    async def from_host(self):
        try:
            while True:
                item = await self.q.get()
                if item is None:
                    if self.w.can_write_eof():
                        self.w.write_eof()
                    self.w_done = True
                    self.maybe_done()
                    return
                self.w.write(item)
                await self.w.drain()
                self.consumed += len(item)
                if self.consumed >= self.window // 2 and not self.eof_recv:
                    self.n.send(P.credit(self.id, self.consumed))
                    self.rx_outstanding += self.consumed
                    self.consumed = 0
        except (ConnectionError, OSError):
            self.n.send(P.close(self.id, P.Close.RESET))
            self.drop()

    def maybe_done(self):
        if self.eof_sent and self.eof_recv and self.w_done:
            self.drop()

    def drop(self):
        self.n.channels.pop(self.id, None)
        for t in self.tasks:
            if t is not asyncio.current_task():
                t.cancel()
        try:
            self.w.transport.abort()
            self.w.close()
        except Exception:
            pass


class FakeNode:
    def __init__(self, transport, session_id=0xA5A5A5A5):
        self.t = transport
        self.session_id = session_id
        self.state = P.NodeState.NEEDS_CONFIG
        self.window = P.DEFAULT_WINDOW
        self.listeners = {}      # id -> asyncio.Server
        self.listener_cfg = {}
        self.channels = {}
        self.pending = {}        # host-chosen channel id -> connect task
        self.forwards = {}
        self.received_config = None
        self.reboot_mode = None
        self.pings = 0
        self._asm = P.ConfigAssembler()
        self._df = P.Deframer()
        self.silent = False      # when True, ignore the host (simulates a hung node)
        self._task = asyncio.ensure_future(self._rx())

    def send(self, f):
        try:
            self.t.write(P.encode(f))
        except LinkClosed:
            pass

    def inject(self, raw):
        self.t.write(raw)

    async def _rx(self):
        try:
            while True:
                for f in self._df.feed(await self.t.read()):
                    if not self.silent:
                        await self._handle(f)
        except LinkClosed:
            pass
        finally:
            await self.stop()

    async def stop(self):
        for s in self.listeners.values():
            s.close()
        self.listeners.clear()
        for c in list(self.channels.values()):
            c.drop()
        self._task.cancel()

    def _reset_channels(self):
        for t in self.pending.values():
            t.cancel()
        self.pending.clear()
        for c in list(self.channels.values()):
            c.drop()

    async def _handle(self, f):
        t = f.type
        if t == P.T.HELLO:
            self._reset_channels()
            self.state = P.NodeState.NEEDS_CONFIG
            self.send(P.hello(P.Role.NODE, self.state, self.session_id, "fake-node 0"))
        elif t == P.T.PING:
            self.pings += 1
            self.send(P.Frame(P.T.PONG, 0, f.payload))
        elif t == P.T.CONFIG:
            blob = self._asm.add(f)
            if blob is not None:
                await self._apply(blob)
        elif t == P.T.OPEN:
            self._on_open(f)
        elif t == P.T.REBOOT:
            self.reboot_mode = f.payload[0]
        elif t == P.T.DATA:
            c = self.channels.get(f.chan)
            if not c:
                self.send(P.close(f.chan, P.Close.NO_CHANNEL))
                return
            c.rx_outstanding -= len(f.payload)
            if c.rx_outstanding < 0:
                self.send(P.close(c.id, P.Close.PROTO))
                c.drop()
                return
            c.q.put_nowait(f.payload)
        elif t == P.T.CREDIT:
            c = self.channels.get(f.chan)
            if c:
                c.credit_out += P.parse_credit(f.payload)
                c.ev.set()
        elif t == P.T.CLOSE:
            task = self.pending.pop(f.chan, None)
            if task:
                task.cancel()
                return
            c = self.channels.get(f.chan)
            if not c:
                return
            reason = P.parse_close(f.payload)
            if reason == P.Close.NO_CHANNEL and c.eof_sent and c.eof_recv:
                return
            if reason == P.Close.EOF:
                c.eof_recv = True
                c.q.put_nowait(None)
                c.maybe_done()
            else:
                c.drop()

    async def _apply(self, blob):
        try:
            cfg = json.loads(blob)
            assert cfg["v"] == 1
            self.window = cfg["link"]["window"]
            self.forwards = {x["id"]: x for x in cfg.get("forwards", [])}
            want = {l["id"]: l for l in cfg["listeners"]}
        except Exception as e:
            self.send(P.Frame(P.T.CONFIG_RES, 0, json.dumps({"ok": False, "errors": [str(e)]}).encode()))
            return
        self.received_config = cfg
        res = []
        for lid in list(self.listeners):
            if lid not in want or self.listener_cfg[lid] != want[lid]:
                self.listeners.pop(lid).close()
                for c in list(self.channels.values()):
                    pass
        for lid, l in want.items():
            if lid in self.listeners:
                res.append({"id": lid, "status": "ok"})
                continue
            try:
                srv = await asyncio.start_server(lambda r, w, lid=lid: self._on_client(lid, r, w), "127.0.0.1", l["port"])
                self.listeners[lid] = srv
                self.listener_cfg[lid] = l
                res.append({"id": lid, "status": "ok"})
            except OSError:
                res.append({"id": lid, "status": "port_in_use"})
        ok = all(r["status"] == "ok" for r in res)
        if ok:
            self.state = P.NodeState.RUNNING
        self.send(P.Frame(P.T.CONFIG_RES, 0, json.dumps(
            {"ok": ok, "wifi": "ok", "hotspot": "ok", "listeners": res, "errors": []}).encode()))
        self.send(P.Frame(P.T.STATUS, 0, json.dumps({"state": "running" if ok else "needs_config", "mode": "sta",
                                                     "ip": "127.0.0.1", "version": "fake-node 0"}).encode()))

    def _on_open(self, f):
        cid = f.chan
        fid = P.parse_open(f.payload)
        fwd = self.forwards.get(fid)
        if self.state != P.NodeState.RUNNING:
            status = P.Open.NOT_READY
        elif fwd is None:
            status = P.Open.UNKNOWN_FORWARD
        elif cid in self.channels or cid in self.pending:
            status = P.Open.BAD_REQUEST
        elif len(self.channels) + len(self.pending) >= P.MAX_CHANNELS:
            status = P.Open.NO_CAPACITY
        else:
            self.pending[cid] = asyncio.ensure_future(self._connect(cid, fwd))
            return
        self.send(P.open_res(cid, status))

    async def _connect(self, cid, fwd):
        try:
            try:
                r, w = await asyncio.wait_for(asyncio.open_connection(fwd["host"], fwd["port"]), 10)
            except ConnectionRefusedError:
                status = P.Open.REFUSED
            except socket.gaierror:
                status = P.Open.DNS
            except (OSError, asyncio.TimeoutError):
                status = P.Open.UNREACHABLE
            else:
                self.pending.pop(cid, None)
                self.channels[cid] = FChan(self, cid, r, w, self.window)
                self.send(P.open_res(cid, P.Open.OK))
                return
            self.pending.pop(cid, None)
            self.send(P.open_res(cid, status))
        except asyncio.CancelledError:
            pass

    async def _on_client(self, lid, reader, writer):
        free = [i for i in range(1, P.MAX_CHANNELS + 1) if i not in self.channels]
        l = self.listener_cfg[lid]
        if self.state != P.NodeState.RUNNING or not free:
            if l["http"]:
                writer.write(RESP_503)
                try:
                    await writer.drain()
                except OSError:
                    pass
            writer.close()
            return
        cid = free[0]
        peer = writer.get_extra_info("peername")
        ip = bytes(int(x) for x in peer[0].split("."))
        self.channels[cid] = FChan(self, cid, reader, writer, self.window)
        self.send(P.accept(cid, lid, ip, peer[1]))
