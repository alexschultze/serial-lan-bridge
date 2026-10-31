import asyncio
import os
import socket
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from fake_node import FakeNode  # noqa: E402
from tether_host import protocol as P  # noqa: E402
from tether_host.config import parse_config  # noqa: E402
from tether_host.link import loopback_pair  # noqa: E402
from tether_host.session import Session  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def make_cfg(listen_port, target_port, protocol="http", window=4096):
    return parse_config({
        "link": {"window": window},
        "wifi": {"network": [{"ssid": "x", "psk": "password1"}]},
        "listener": [{"id": 1, "port": listen_port, "target": f"127.0.0.1:{target_port}", "protocol": protocol}],
        "host": {"connect_timeout": 2},
    })


class Rig:
    """Session + FakeNode over a loopback transport, reconnecting to a fresh node when the link drops."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.nodes = []
        self.session = Session(cfg, self._open)
        self.task = None

    async def _open(self):
        host_t, node_t = loopback_pair()
        node = FakeNode(node_t, session_id=len(self.nodes) + 1)
        self.nodes.append(node)
        return host_t

    async def __aenter__(self):
        self.task = asyncio.ensure_future(self.session.run_forever())
        await asyncio.wait_for(self.session.running.wait(), 5)
        return self

    async def __aexit__(self, *a):
        self.session.stop()
        await asyncio.wait_for(self.task, 5)
        for n in self.nodes:
            await n.stop()

    @property
    def node(self):
        return self.nodes[-1]


async def echo_server(handler):
    srv = await asyncio.start_server(handler, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1]


class E2E(unittest.IsolatedAsyncioTestCase):
    async def test_config_reaches_node_without_targets(self):
        lp = free_port()
        async with Rig(make_cfg(lp, 9)) as rig:
            c = rig.node.received_config
            self.assertEqual(c["listeners"], [{"id": 1, "port": lp, "http": True, "allow": []}])
            self.assertNotIn("target", str(c))
            self.assertEqual(c["wifi"]["networks"][0]["ssid"], "x")
            self.assertIsNone(c["hotspot"])

    async def test_streaming_is_not_buffered(self):
        async def api(r, w):
            await r.readuntil(b"\r\n\r\n")
            w.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
            for i in range(5):
                w.write(f"data: {i}\n\n".encode())
                await w.drain()
                await asyncio.sleep(0.15)
            w.close()
        srv, tp = await echo_server(api)
        lp = free_port()
        async with Rig(make_cfg(lp, tp)) as rig:
            r, w = await asyncio.open_connection("127.0.0.1", lp)
            w.write(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n\r\n")
            await r.readuntil(b"\r\n\r\n")
            t0 = asyncio.get_running_loop().time()
            first = await r.readuntil(b"\n\n")
            t_first = asyncio.get_running_loop().time() - t0
            self.assertEqual(first, b"data: 0\n\n")
            self.assertLess(t_first, 0.4)     # arrives with the first chunk, not after the whole stream
            rest = await r.read()
            self.assertEqual(rest, b"".join(f"data: {i}\n\n".encode() for i in range(1, 5)))
            w.close()
        srv.close()

    async def test_large_bidirectional_transfer(self):
        payload = os.urandom(1_500_000)

        async def echo(r, w):
            while True:
                d = await r.read(65536)
                if not d:
                    break
                w.write(d)
                await w.drain()
            w.close()
        srv, tp = await echo_server(echo)
        lp = free_port()
        async with Rig(make_cfg(lp, tp)) as rig:
            r, w = await asyncio.open_connection("127.0.0.1", lp)

            async def send():
                w.write(payload)
                await w.drain()
                w.write_eof()
            st = asyncio.ensure_future(send())
            got = await asyncio.wait_for(r.read(-1), 20)
            await st
            self.assertEqual(len(got), len(payload))
            self.assertEqual(got, payload)
        srv.close()

    async def test_half_close_client_then_reply(self):
        async def api(r, w):
            body = await r.read(-1)           # until the client's EOF
            w.write(b"got:" + body)
            await w.drain()
            w.close()
        srv, tp = await echo_server(api)
        lp = free_port()
        async with Rig(make_cfg(lp, tp)) as rig:
            r, w = await asyncio.open_connection("127.0.0.1", lp)
            w.write(b"hello")
            w.write_eof()
            self.assertEqual(await asyncio.wait_for(r.read(-1), 5), b"got:hello")
        srv.close()

    async def test_unreachable_target_gives_502(self):
        lp = free_port()
        async with Rig(make_cfg(lp, free_port())) as rig:
            r, w = await asyncio.open_connection("127.0.0.1", lp)
            w.write(b"GET / HTTP/1.1\r\n\r\n")
            data = await asyncio.wait_for(r.read(-1), 5)
            self.assertTrue(data.startswith(b"HTTP/1.1 502"), data)
            w.close()

    async def test_tcp_listener_failure_resets(self):
        lp = free_port()
        async with Rig(make_cfg(lp, free_port(), protocol="tcp")) as rig:
            r, w = await asyncio.open_connection("127.0.0.1", lp)
            w.write(b"x")
            try:
                data = await asyncio.wait_for(r.read(-1), 5)
            except ConnectionError:
                data = b""
            self.assertEqual(data, b"")

    async def test_capacity_17th_gets_503(self):
        async def hold(r, w):
            await r.read(-1)
        srv, tp = await echo_server(hold)
        lp = free_port()
        async with Rig(make_cfg(lp, tp)) as rig:
            conns = []
            for _ in range(P.MAX_CHANNELS):
                conns.append(await asyncio.open_connection("127.0.0.1", lp))
            await asyncio.sleep(0.3)
            self.assertEqual(len(rig.node.channels), P.MAX_CHANNELS)
            r, w = await asyncio.open_connection("127.0.0.1", lp)
            data = await asyncio.wait_for(r.read(-1), 5)
            self.assertTrue(data.startswith(b"HTTP/1.1 503"), data)
            for _, cw in conns:
                cw.close()
            await asyncio.sleep(0.3)
        srv.close()

    async def test_recovers_after_node_reset(self):
        async def api(r, w):
            await r.readuntil(b"\r\n\r\n")
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            await w.drain()
            w.close()
        srv, tp = await echo_server(api)
        lp = free_port()
        async with Rig(make_cfg(lp, tp)) as rig:
            await rig.node.stop()
            await rig.node.t.close()                   # cable pulled
            await asyncio.sleep(0.2)
            await asyncio.wait_for(rig.session.running.wait(), 8)
            self.assertGreaterEqual(len(rig.nodes), 2)
            for _ in range(50):
                try:
                    r, w = await asyncio.open_connection("127.0.0.1", lp)
                    break
                except OSError:
                    await asyncio.sleep(0.1)
            w.write(b"GET / HTTP/1.1\r\n\r\n")
            self.assertTrue((await asyncio.wait_for(r.read(-1), 5)).endswith(b"ok"))
        srv.close()

    async def test_framing_error_resets_session(self):
        lp = free_port()
        async with Rig(make_cfg(lp, free_port())) as rig:
            bad = bytearray(P.encode(P.Frame(P.T.PONG, 0, b"\0\0\0\0")))
            bad[3] ^= 0x40
            rig.node.inject(bytes(bad))
            await asyncio.sleep(0.5)
            await asyncio.wait_for(rig.session.running.wait(), 8)
            self.assertGreaterEqual(len(rig.nodes), 2)
            self.assertGreaterEqual(rig.session.stats["crc_errors"], 1)

    async def test_silent_node_is_detected(self):
        import tether_host.session as S
        old = S.PONG_TIMEOUT, S.PING_EVERY
        S.PONG_TIMEOUT, S.PING_EVERY = 1.0, 0.3
        try:
            lp = free_port()
            async with Rig(make_cfg(lp, free_port())) as rig:
                rig.node.silent = True
                await asyncio.sleep(1.8)
                self.assertGreaterEqual(len(rig.nodes), 2)
        finally:
            S.PONG_TIMEOUT, S.PING_EVERY = old


if __name__ == "__main__":
    unittest.main()
