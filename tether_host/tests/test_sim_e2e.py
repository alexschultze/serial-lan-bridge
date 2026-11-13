"""End-to-end: tether-host (real SerialTransport) <-> the real C engine running as tether_sim behind a pty.
Skipped when no C compiler/make is available."""
import asyncio
import os
import shutil
import signal
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
from test_e2e import echo_server, free_port, make_cfg  # noqa: E402
from tether_host import protocol as P  # noqa: E402
from tether_host.config import parse_config  # noqa: E402
from tether_host.link import SerialTransport  # noqa: E402
from tether_host.session import Session  # noqa: E402

SIM_DIR = os.path.join(HERE, "..", "..", "tether-node", "sim")
SIM = os.path.join(SIM_DIR, "build", "tether_sim")


def build_sim():
    if os.name != "posix" or not shutil.which("make") or not (shutil.which("gcc") or shutil.which("cc")):
        return False
    return subprocess.run(["make", "-s", "-C", SIM_DIR, "build/tether_sim"], capture_output=True).returncode == 0


HAVE_SIM = build_sim()


class SimProc:
    def __init__(self, env=None):
        self.proc = subprocess.Popen([SIM], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     env={**os.environ, **(env or {})})
        line = self.proc.stdout.readline().strip()
        assert line.startswith("PTY "), line
        self.path = line[4:]

    def kill(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        err = self.proc.stderr.read()
        self.proc.stderr.close()
        self.proc.stdout.close()
        return err


class SimRig:
    def __init__(self, cfg, env=None):
        self.cfg, self.env = cfg, env
        self.sim = None
        self.opens = 0
        self.errors = ""
        self.session = Session(cfg, self._open)

    async def _open(self):
        if self.sim is None or self.sim.proc.poll() is not None:
            self.sim = SimProc(self.env)
        self.opens += 1
        return SerialTransport(self.sim.path)

    async def __aenter__(self):
        self.task = asyncio.ensure_future(self.session.run_forever())
        await asyncio.wait_for(self.session.running.wait(), 10)
        return self

    async def __aexit__(self, *a):
        self.session.stop()
        await asyncio.wait_for(self.task, 10)
        if self.sim:
            self.errors = self.sim.kill()


@unittest.skipUnless(HAVE_SIM, "no C compiler / make, or not POSIX")
class SimE2E(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_through_the_c_engine(self):
        async def api(r, w):
            await r.readuntil(b"\r\n\r\n")
            w.write(b"HTTP/1.1 200 OK\r\n\r\n")
            for i in range(4):
                w.write(f"data: {i}\n\n".encode())
                await w.drain()
                await asyncio.sleep(0.15)
            w.close()
        srv, tp = await echo_server(api)
        lp = free_port()
        async with SimRig(make_cfg(lp, tp)):
            r, w = await asyncio.open_connection("127.0.0.1", lp)
            w.write(b"POST /v1/chat/completions HTTP/1.1\r\n\r\n")
            await r.readuntil(b"\r\n\r\n")
            t0 = asyncio.get_running_loop().time()
            self.assertEqual(await r.readuntil(b"\n\n"), b"data: 0\n\n")
            self.assertLess(asyncio.get_running_loop().time() - t0, 0.4)
            self.assertEqual(await r.read(), b"".join(f"data: {i}\n\n".encode() for i in range(1, 4)))
            w.close()
        srv.close()

    async def _transfer(self, window, size):
        payload = os.urandom(size)

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
        async with SimRig(make_cfg(lp, tp, window=window)):
            r, w = await asyncio.open_connection("127.0.0.1", lp)

            async def send():
                w.write(payload)
                await w.drain()
                w.write_eof()
            st = asyncio.ensure_future(send())
            t0 = asyncio.get_running_loop().time()
            got = await asyncio.wait_for(r.read(-1), 60)
            await st
            self.assertEqual(got, payload)
            return size * 2 / (asyncio.get_running_loop().time() - t0)
        srv.close()

    async def test_large_transfer_default_window(self):
        rate = await self._transfer(4096, 2_000_000)
        print(f"\n  pty throughput (up+down, 2 MB each way): {rate / 1e6:.2f} MB/s")

    async def test_large_transfer_small_window(self):
        await self._transfer(1024, 300_000)

    async def test_half_close(self):
        async def api(r, w):
            body = await r.read(-1)
            w.write(b"got:" + body)
            await w.drain()
            w.close()
        srv, tp = await echo_server(api)
        lp = free_port()
        async with SimRig(make_cfg(lp, tp)):
            r, w = await asyncio.open_connection("127.0.0.1", lp)
            w.write(b"hello")
            w.write_eof()
            self.assertEqual(await asyncio.wait_for(r.read(-1), 5), b"got:hello")
        srv.close()

    async def test_502_for_dead_target(self):
        lp = free_port()
        async with SimRig(make_cfg(lp, free_port())):
            r, w = await asyncio.open_connection("127.0.0.1", lp)
            w.write(b"GET / HTTP/1.1\r\n\r\n")
            self.assertTrue((await asyncio.wait_for(r.read(-1), 5)).startswith(b"HTTP/1.1 502"))
            w.close()

    async def test_17th_connection_gets_503(self):
        async def hold(r, w):
            await r.read(-1)
        srv, tp = await echo_server(hold)
        lp = free_port()
        async with SimRig(make_cfg(lp, tp)) as rig:
            conns = [await asyncio.open_connection("127.0.0.1", lp) for _ in range(P.MAX_CHANNELS)]
            await asyncio.sleep(0.5)
            self.assertEqual(len(rig.session.channels), P.MAX_CHANNELS)
            r, w = await asyncio.open_connection("127.0.0.1", lp)
            self.assertTrue((await asyncio.wait_for(r.read(-1), 5)).startswith(b"HTTP/1.1 503"))
            for _, cw in conns:
                cw.close()
        srv.close()

    async def test_allow_list_refuses(self):
        lp = free_port()
        cfg = parse_config({"wifi": {"network": [{"ssid": "x"}]}, "host": {"connect_timeout": 2},
                            "listener": [{"id": 1, "port": lp, "target": "127.0.0.1:9", "allow": ["10.9.9.9/32"]}]})
        async with SimRig(cfg):
            try:
                r, w = await asyncio.open_connection("127.0.0.1", lp)
                data = await asyncio.wait_for(r.read(-1), 5)
            except ConnectionError:   # reset before or right after the handshake: refused
                data = b""
            self.assertEqual(data, b"")

    async def test_never_used_timeout(self):
        lp = free_port()
        cfg = parse_config({"link": {"never_used_timeout": 1}, "wifi": {"network": [{"ssid": "x"}]},
                            "listener": [{"id": 1, "port": lp, "target": "127.0.0.1:9", "protocol": "tcp"}]})
        async with SimRig(cfg):
            r, w = await asyncio.open_connection("127.0.0.1", lp)
            t0 = asyncio.get_running_loop().time()
            try:
                await asyncio.wait_for(r.read(-1), 6)
            except ConnectionError:
                pass
            self.assertLess(asyncio.get_running_loop().time() - t0, 5)

    async def test_node_restart_recovers(self):
        async def api(r, w):
            await r.readuntil(b"\r\n\r\n")
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            await w.drain()
            w.close()
        srv, tp = await echo_server(api)
        lp = free_port()
        async with SimRig(make_cfg(lp, tp)) as rig:
            rig.sim.kill()                       # node dies (power loss)
            await asyncio.sleep(0.3)
            await asyncio.wait_for(rig.session.running.wait(), 15)
            self.assertGreaterEqual(rig.opens, 2)
            for _ in range(50):
                try:
                    r, w = await asyncio.open_connection("127.0.0.1", lp)
                    break
                except OSError:
                    await asyncio.sleep(0.1)
            w.write(b"GET / HTTP/1.1\r\n\r\n")
            self.assertTrue((await asyncio.wait_for(r.read(-1), 5)).endswith(b"ok"))
        srv.close()

    async def test_status_reaches_host(self):
        lp = free_port()
        async with SimRig(make_cfg(lp, free_port())) as rig:
            await asyncio.sleep(0.3)
            st = rig.session.node_status
            self.assertEqual((st.get("state"), st.get("mode"), st.get("ssid")), ("running", "sta", "sim"))

    async def test_wifi_problem_and_address_are_logged(self):
        lp = free_port()
        with self.assertLogs("tether.session", "INFO") as cm:
            async with SimRig(make_cfg(lp, free_port()), env={"SIM_WIFI_NOTE": "'NetworkB': authentication failed (wrong password?)"}) as rig:
                await asyncio.sleep(0.3)
                self.assertIn("wrong password", rig.session.node_status.get("note", ""))
        text = "\n".join(cm.output)
        self.assertIn("joined 'sim', address 127.0.0.1", text)
        self.assertIn("WARNING:tether.session:node Wi-Fi: 'NetworkB': authentication failed (wrong password?)", text)

    async def test_rejected_wifi_keeps_node_unconfigured(self):
        lp = free_port()
        rig = SimRig(make_cfg(lp, free_port()), env={"SIM_WIFI_FAIL": "1"})
        task = asyncio.ensure_future(rig.session.run_forever())
        await asyncio.sleep(2.0)
        self.assertFalse(rig.session.running.is_set())
        rig.session.stop()
        await asyncio.wait_for(task, 10)
        rig.sim.kill()


@unittest.skipUnless(HAVE_SIM, "no C compiler / make, or not POSIX")
class SimForwards(unittest.IsolatedAsyncioTestCase):
    """Local port on the PC -> the C engine connects to an endpoint on 'its' network (loopback here)."""

    async def test_forward_by_ip_streams(self):
        async def endpoint(r, w):
            await r.readuntil(b"\r\n\r\n")
            w.write(b"HTTP/1.1 200 OK\r\n\r\n")
            for i in range(4):
                w.write(f"data: {i}\n\n".encode())
                await w.drain()
                await asyncio.sleep(0.15)
            w.close()
        srv, tp = await echo_server(endpoint)
        fp = free_port()
        async with SimRig(make_cfg(free_port(), 9, forwards=[(fp, "127.0.0.1", tp)])):
            r, w = await asyncio.open_connection("127.0.0.1", fp)
            w.write(b"GET /events HTTP/1.1\r\n\r\n")
            await r.readuntil(b"\r\n\r\n")
            t0 = asyncio.get_running_loop().time()
            self.assertEqual(await r.readuntil(b"\n\n"), b"data: 0\n\n")
            self.assertLess(asyncio.get_running_loop().time() - t0, 0.4)
            self.assertEqual(await r.read(), b"".join(f"data: {i}\n\n".encode() for i in range(1, 4)))
            w.close()
        srv.close()

    async def test_forward_by_name_uses_the_resolver_thread(self):
        async def api(r, w):
            await r.readuntil(b"\r\n\r\n")
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            await w.drain()
            w.close()
        srv, tp = await echo_server(api)
        fp = free_port()
        async with SimRig(make_cfg(free_port(), 9, forwards=[(fp, "localhost", tp)])):
            r, w = await asyncio.open_connection("127.0.0.1", fp)
            w.write(b"GET / HTTP/1.1\r\n\r\n")
            self.assertTrue((await asyncio.wait_for(r.read(-1), 8)).endswith(b"ok"))
            w.close()
        srv.close()

    async def test_forward_by_name_with_an_eventfd_wakeup(self):
        """Regression: the node hung on its first DNS lookup because it read its wake-up fd in a loop (ESP-IDF eventfds
        never report 'empty'). With a blocking eventfd the old loop blocks the simulator the same way."""
        async def api(r, w):
            await r.readuntil(b"\r\n\r\n")
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            await w.drain()
            w.close()
        srv, tp = await echo_server(api)
        fp = free_port()
        async with SimRig(make_cfg(free_port(), 9, forwards=[(fp, "localhost", tp)]), env={"SIM_WAKE_EVENTFD": "1"}) as rig:
            for _ in range(3):   # several lookups in a row, and the link must stay alive afterwards
                r, w = await asyncio.open_connection("127.0.0.1", fp)
                w.write(b"GET / HTTP/1.1\r\n\r\n")
                self.assertTrue((await asyncio.wait_for(r.read(-1), 8)).endswith(b"ok"))
                w.close()
            await asyncio.sleep(2.5)                     # a few PING/PONG rounds
            self.assertTrue(rig.session.running.is_set())
            self.assertEqual(rig.opens, 1)               # never reconnected
        srv.close()

    async def test_forward_large_both_ways(self):
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
        fp = free_port()
        async with SimRig(make_cfg(free_port(), 9, forwards=[(fp, "127.0.0.1", tp, "tcp")])):
            r, w = await asyncio.open_connection("127.0.0.1", fp)

            async def send():
                w.write(payload)
                await w.drain()
                w.write_eof()
            st = asyncio.ensure_future(send())
            got = await asyncio.wait_for(r.read(-1), 60)
            await st
            self.assertEqual(got, payload)
        srv.close()

    async def test_refused_gives_502(self):
        fp = free_port()
        async with SimRig(make_cfg(free_port(), 9, forwards=[(fp, "127.0.0.1", free_port())])):
            r, w = await asyncio.open_connection("127.0.0.1", fp)
            w.write(b"GET / HTTP/1.1\r\n\r\n")
            self.assertTrue((await asyncio.wait_for(r.read(-1), 8)).startswith(b"HTTP/1.1 502"))
            w.close()

    async def test_unresolvable_name_fails_cleanly(self):
        fp = free_port()
        async with SimRig(make_cfg(free_port(), 9, forwards=[(fp, "does-not-exist.invalid", 80)])):
            r, w = await asyncio.open_connection("127.0.0.1", fp)
            w.write(b"GET / HTTP/1.1\r\n\r\n")
            data = await asyncio.wait_for(r.read(-1), 12)
            self.assertTrue(data.startswith(b"HTTP/1.1 502") or data.startswith(b"HTTP/1.1 504"), data)
            w.close()

    async def test_both_directions_and_capacity(self):
        async def hold(r, w):
            await r.read(-1)
        srv, tp = await echo_server(hold)
        lp, fp = free_port(), free_port()
        async with SimRig(make_cfg(lp, tp, forwards=[(fp, "127.0.0.1", tp, "tcp")])) as rig:
            conns = [await asyncio.open_connection("127.0.0.1", fp) for _ in range(8)]
            conns += [await asyncio.open_connection("127.0.0.1", lp) for _ in range(8)]
            await asyncio.sleep(0.6)
            self.assertEqual(len(rig.session.channels), P.MAX_CHANNELS)        # 8 outbound + 8 inbound share the 16 channels
            r, w = await asyncio.open_connection("127.0.0.1", lp)               # the 17th: 503 from the node
            self.assertTrue((await asyncio.wait_for(r.read(-1), 5)).startswith(b"HTTP/1.1 503"))
            r, w = await asyncio.open_connection("127.0.0.1", fp)               # tcp forward: closed
            try:
                data = await asyncio.wait_for(r.read(-1), 5)
            except ConnectionError:
                data = b""
            self.assertEqual(data, b"")
            for _, cw in conns:
                cw.close()
        srv.close()

    async def test_forward_after_node_restart(self):
        async def api(r, w):
            await r.readuntil(b"\r\n\r\n")
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            await w.drain()
            w.close()
        srv, tp = await echo_server(api)
        fp = free_port()
        async with SimRig(make_cfg(free_port(), 9, forwards=[(fp, "127.0.0.1", tp)])) as rig:
            rig.sim.kill()
            await asyncio.sleep(0.3)
            await asyncio.wait_for(rig.session.running.wait(), 15)
            r, w = await asyncio.open_connection("127.0.0.1", fp)
            w.write(b"GET / HTTP/1.1\r\n\r\n")
            self.assertTrue((await asyncio.wait_for(r.read(-1), 8)).endswith(b"ok"))
        srv.close()


if __name__ == "__main__":
    unittest.main()
