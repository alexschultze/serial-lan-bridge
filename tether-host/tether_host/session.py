"""Link session: handshake, CONFIG push, keepalive, channel dispatch, reconnect."""
import asyncio
import json
import logging
import os
import random
import socket
import time

from . import protocol as P
from .bridge import Channel
from .link import LinkClosed

log = logging.getLogger("tether.session")
nodelog = logging.getLogger("tether.node")

PING_EVERY = 2.0
PONG_TIMEOUT = 6.0
CONFIG_RETRY = 5.0
CONFIG_TRIES = 3
RESEND_MIN = 5.0          # minimum time between CONFIG pushes triggered by node HELLOs
BACKOFF = (0.5, 1, 2, 5)
VERSION = "tether-host 0.1"


class SessionError(Exception):
    pass


class Session:
    def __init__(self, cfg, open_transport, status_path=None):
        """`open_transport` is an async callable returning a connected Transport (raises OSError if unavailable)."""
        self.cfg = cfg
        self._open = open_transport
        self.status_path = status_path
        self.session_id = random.getrandbits(32)
        self.channels = {}
        self.node_status = {}
        self.node_info = {}
        self.running = asyncio.Event()      # set while the node reports RUNNING
        self._stop = False
        self._t = None
        self._deframer = None
        self._fail = None
        self._seq = 0
        self._last_pong = 0.0
        self._node_session = None
        self._last_config = 0.0
        self._cfg_tries = 0
        self._config_ok = False
        self.stats = {"reconnects": 0, "crc_errors": 0}

    # ---- outbound ---------------------------------------------------------------------------
    def send(self, frame):
        t = self._t
        if t is None:
            return
        try:
            t.write(P.encode(frame))
        except LinkClosed:
            self._set_fail("link closed")

    def channel_done(self, ch):
        if self.channels.get(ch.id) is ch:
            del self.channels[ch.id]

    # ---- lifecycle --------------------------------------------------------------------------
    def stop(self):
        self._stop = True
        if self._fail and not self._fail.done():
            self._fail.set_result("stopped")

    async def run_forever(self):
        step = 0
        while not self._stop:
            try:
                transport = await self._open()
            except (OSError, SessionError) as e:
                log.warning("cannot open the node: %s", e)
                await self._sleep(BACKOFF[min(step, len(BACKOFF) - 1)])
                step += 1
                continue
            step = 0
            log.info("link open")
            try:
                await self._run_link(transport)
            except Exception:
                log.exception("session error")
            finally:
                await self._teardown(transport)
            if not self._stop:
                self.stats["reconnects"] += 1
                log.warning("link lost, reconnecting")
                await self._sleep(BACKOFF[0])

    async def _sleep(self, s):
        for _ in range(int(s * 10) or 1):
            if self._stop:
                return
            await asyncio.sleep(0.1)

    def _set_fail(self, why):
        if self._fail and not self._fail.done():
            self._fail.set_result(why)

    async def _run_link(self, transport):
        self._t = transport
        self._deframer = P.Deframer()
        self._fail = asyncio.get_running_loop().create_future()
        self._node_session = None
        self._config_ok = False
        self._last_config = 0.0
        self._last_pong = time.monotonic()
        self.running.clear()
        tasks = [asyncio.ensure_future(self._rx_loop()), asyncio.ensure_future(self._tick_loop())]
        self.send(P.hello(P.Role.HOST, P.NodeState.NEEDS_CONFIG, self.session_id, VERSION))
        why = await self._fail
        log.info("session ended: %s", why)
        for t in tasks:
            t.cancel()

    async def _teardown(self, transport):
        self.running.clear()
        for ch in list(self.channels.values()):
            ch.abort()
        self.channels.clear()
        self._t = None
        try:
            await transport.close()
        except Exception:
            pass
        self._write_status(link="down")

    # ---- receive ----------------------------------------------------------------------------
    async def _rx_loop(self):
        try:
            while True:
                chunk = await self._t.read()
                before = self._deframer.errors
                for f in self._deframer.feed(chunk):
                    self._handle(f)
                if self._deframer.errors != before:
                    self.stats["crc_errors"] += self._deframer.errors - before
                    log.warning("framing error on the link; resetting the session")
                    self._set_fail("framing error")
        except LinkClosed as e:
            self._set_fail(str(e))

    def _handle(self, f):
        t = f.type
        if t == P.T.HELLO:
            self._on_hello(f)
        elif t == P.T.PONG:
            self._last_pong = time.monotonic()
        elif t == P.T.CONFIG_RES:
            self._on_config_res(f)
        elif t == P.T.STATUS:
            try:
                self.node_status = json.loads(f.payload)
            except ValueError:
                return
            self._write_status()
        elif t == P.T.LOG:
            if f.payload:
                nodelog.log((logging.ERROR, logging.WARNING, logging.INFO, logging.DEBUG)[min(f.payload[0], 3)],
                            "%s", f.payload[1:].decode(errors="replace").rstrip())
        elif t == P.T.ACCEPT:
            self._on_accept(f)
        elif t in (P.T.DATA, P.T.CREDIT, P.T.CLOSE):
            ch = self.channels.get(f.chan)
            if ch is None:
                if t != P.T.CLOSE:
                    self.send(P.close(f.chan, P.Close.NO_CHANNEL))
                return
            try:
                if t == P.T.DATA:
                    ch.on_data(f.payload)
                elif t == P.T.CREDIT:
                    ch.on_credit(P.parse_credit(f.payload))
                else:
                    ch.on_close(P.parse_close(f.payload))
            except P.FrameError:
                self.send(P.close(f.chan, P.Close.PROTO))
                ch.abort()

    def _on_hello(self, f):
        try:
            h = P.parse_hello(f.payload)
        except (P.FrameError, ValueError):
            log.warning("bad HELLO from the node")
            return
        if h["role"] != P.Role.NODE:
            return
        if h["proto"] != P.PROTO_VERSION:
            log.error("protocol version mismatch: node %d, host %d", h["proto"], P.PROTO_VERSION)
            self._set_fail("protocol mismatch")
            return
        self.node_info = h
        changed = h["session"] != self._node_session
        if changed or h["state"] == P.NodeState.NEEDS_CONFIG:
            if changed:
                log.info("node %s connected (session %08x)", h["version"], h["session"])
            for ch in list(self.channels.values()):
                ch.abort()
            self._node_session = h["session"]
            self.running.clear()
            now = time.monotonic()
            if changed or now - self._last_config >= RESEND_MIN:
                self._push_config()

    def _push_config(self):
        self._last_config = time.monotonic()
        self._cfg_tries += 1
        self._config_ok = False
        for fr in P.config_frames(self.cfg.node_json()):
            self.send(fr)

    def _on_config_res(self, f):
        try:
            res = json.loads(f.payload)
        except ValueError:
            return
        if res.get("ok"):
            self._config_ok = True
            self._cfg_tries = 0
            self.running.set()
            log.info("node configured: wifi=%s hotspot=%s listeners=%s", res.get("wifi"), res.get("hotspot"),
                     ", ".join(f"{x['id']}:{x['status']}" for x in res.get("listeners", [])))
        else:
            log.error("node rejected the configuration: %s", res)

    def _on_accept(self, f):
        try:
            lid, ip, port = P.parse_accept(f.payload)
        except P.FrameError:
            return
        lst = self.cfg.listener(lid)
        if lst is None or not self.running.is_set() or f.chan in self.channels:
            self.send(P.close(f.chan, P.Close.RESET))
            return
        peer = f"{socket.inet_ntoa(ip)}:{port}"
        log.info("connection from %s on listener %d -> %s:%d", peer, lid, lst.target_host, lst.target_port)
        self.channels[f.chan] = Channel(self, f.chan, lst, peer, self.cfg.link.window, self.cfg.host.connect_timeout)

    # ---- periodic ---------------------------------------------------------------------------
    async def _tick_loop(self):
        while True:
            await asyncio.sleep(PING_EVERY)
            now = time.monotonic()
            if now - self._last_pong > PONG_TIMEOUT:
                log.warning("no answer from the node for %.0f s", PONG_TIMEOUT)
                self._set_fail("node silent")
                return
            self._seq = (self._seq + 1) & 0xFFFFFFFF
            self.send(P.Frame(P.T.PING, 0, self._seq.to_bytes(4, "little")))
            if not self._config_ok and now - self._last_config > CONFIG_RETRY:
                if self._cfg_tries >= CONFIG_TRIES:
                    log.error("the node does not accept the configuration; reconnecting")
                    self._cfg_tries = 0
                    self._set_fail("config not acknowledged")
                    return
                if self._node_session is not None:
                    self._push_config()

    def _write_status(self, link="up"):
        if not self.status_path:
            return
        doc = {"updated": time.time(), "pid": os.getpid(), "link": link, "node": self.node_status,
               "host": {"channels": len(self.channels), **self.stats}}
        tmp = self.status_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.status_path), exist_ok=True)
            with open(tmp, "w") as fh:
                json.dump(doc, fh)
            os.replace(tmp, self.status_path)
        except OSError:
            pass
