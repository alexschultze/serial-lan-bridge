import asyncio
import unittest

from tether_host.config import ConfigError, parse_config
from tether_host.httprewrite import HostRewriter
from test_e2e import Rig, echo_server, free_port, make_cfg


def feed_all(rw, data, step):
    """Feeds `data` in pieces of `step` bytes and returns everything that came out."""
    out = b"".join(rw.feed(data[i:i + step]) for i in range(0, len(data), step))
    return out + rw.flush()


class RewriterTests(unittest.TestCase):
    REQ = b"GET /a HTTP/1.1\r\nHost: 127.0.0.1:9000\r\nAccept: */*\r\n\r\n"
    WANT = b"GET /a HTTP/1.1\r\nHost: example.com\r\nAccept: */*\r\n\r\n"

    def test_simple_request(self):
        self.assertEqual(HostRewriter("example.com").feed(self.REQ), self.WANT)

    def test_any_split_gives_the_same_result(self):
        for step in (1, 2, 3, 7, 1000):
            self.assertEqual(feed_all(HostRewriter("example.com"), self.REQ * 3, step), self.WANT * 3, step)

    def test_header_name_is_case_insensitive_and_only_the_first_host_changes(self):
        out = HostRewriter("h").feed(b"GET / HTTP/1.1\r\nhOsT:   x\r\nX-Host: keep\r\n\r\n")
        self.assertEqual(out, b"GET / HTTP/1.1\r\nHost: h\r\nX-Host: keep\r\n\r\n")

    def test_no_host_header_stays_as_it_is(self):
        req = b"GET / HTTP/1.0\r\n\r\n"
        self.assertEqual(HostRewriter("h").feed(req), req)

    def test_content_length_body_is_not_parsed(self):
        body = b"Host: inside\r\n\r\nGET / HTTP/1.1\r\nHost: also-inside\r\n\r\n"
        req = b"POST /p HTTP/1.1\r\nHost: a\r\nContent-Length: %d\r\n\r\n" % len(body) + body
        want = req.replace(b"Host: a", b"Host: h", 1)
        for step in (1, 5, 1000):
            self.assertEqual(feed_all(HostRewriter("h"), req + self.REQ, step), want + self.WANT.replace(b"example.com", b"h"))

    def test_chunked_body_then_next_request(self):
        body = b"5\r\nHost:\r\n3;ext=1\r\nabc\r\n0\r\nTrailer: x\r\n\r\n"
        req = b"POST /p HTTP/1.1\r\nHost: a\r\nTransfer-Encoding: chunked\r\n\r\n" + body
        want = req.replace(b"Host: a", b"Host: h", 1) + self.WANT.replace(b"example.com", b"h")
        for step in (1, 4, 1000):
            self.assertEqual(feed_all(HostRewriter("h"), req + self.REQ, step), want, step)

    def test_upgrade_switches_to_passthrough(self):
        req = b"GET /ws HTTP/1.1\r\nHost: a\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
        rest = b"\x81\x05Host: a\r\n\r\n"
        out = feed_all(HostRewriter("h"), req + rest, 3)
        self.assertEqual(out, req.replace(b"Host: a", b"Host: h") + rest)

    def test_non_http_bytes_pass_through(self):
        junk = bytes(range(256)) * 4
        self.assertEqual(feed_all(HostRewriter("h"), junk, 10), junk)

    def test_oversized_head_gives_up_and_passes_through(self):
        data = b"GET / HTTP/1.1\r\nX: " + b"a" * 70000
        self.assertEqual(feed_all(HostRewriter("h"), data, 4096), data)

    def test_incomplete_head_is_released_at_the_end(self):
        rw = HostRewriter("h")
        self.assertEqual(rw.feed(b"GET / HTTP/1.1\r\nHost: a\r\n"), b"")
        self.assertEqual(rw.flush(), b"GET / HTTP/1.1\r\nHost: a\r\n")


class ConfigTests(unittest.TestCase):
    def cfg(self, **extra):
        fw = {"id": 1, "listen": 9000, "target": "example.com:80", **extra}
        return parse_config({"forward": [fw], "wifi": {"network": [{"ssid": "x", "psk": "password1"}]}})

    def test_values(self):
        self.assertIsNone(self.cfg().forwards[0].host_value)
        self.assertEqual(self.cfg(host_header="auto").forwards[0].host_value, "example.com")
        self.assertEqual(self.cfg(host_header="auto", target="example.com:8080").forwards[0].host_value, "example.com:8080")
        self.assertEqual(self.cfg(host_header="fritz.box").forwards[0].host_value, "fritz.box")

    def test_rejects_bad_values(self):
        for bad in ("a b", "x\r\ny", 5, "é"):
            with self.assertRaises(ConfigError, msg=repr(bad)):
                self.cfg(host_header=bad)
        with self.assertRaises(ConfigError):
            self.cfg(host_header="auto", protocol="tcp")


class ForwardRewriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_forward_rewrites_host_of_each_keepalive_request(self):
        seen = []

        async def endpoint(r, w):
            for _ in range(2):
                head = await r.readuntil(b"\r\n\r\n")
                seen.append(head)
                await r.readexactly(int(head.split(b"Content-Length: ")[1].split(b"\r\n")[0]))
                w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                await w.drain()
            w.close()

        srv, tp = await echo_server(endpoint)
        fp = free_port()
        async with Rig(make_cfg(free_port(), 9, forwards=[(fp, "127.0.0.1", tp, "http", "site.example")])) as rig:
            r, w = await asyncio.open_connection("127.0.0.1", fp)
            for i in range(2):
                w.write(b"POST /x HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Length: 4\r\n\r\nbody" % fp)
                self.assertEqual(await r.readuntil(b"ok"), b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            w.close()
        srv.close()
        self.assertEqual(len(seen), 2)
        for head in seen:
            self.assertIn(b"Host: site.example\r\n", head)
            self.assertNotIn(b"127.0.0.1", head)

    async def test_long_rewritten_head_respects_the_window(self):
        got = bytearray()

        async def endpoint(r, w):
            got.extend(await r.readuntil(b"\r\n\r\n"))
            w.write(b"HTTP/1.1 204 No Content\r\n\r\n")
            await w.drain()
            w.close()

        srv, tp = await echo_server(endpoint)
        fp = free_port()
        long_name = "a" * 60 + ".example.com"
        async with Rig(make_cfg(free_port(), 9, window=1024, forwards=[(fp, "127.0.0.1", tp, "http", long_name)])) as rig:
            r, w = await asyncio.open_connection("127.0.0.1", fp)
            w.write(b"GET / HTTP/1.1\r\nHost: x\r\nCookie: " + b"c" * 900 + b"\r\n\r\n")
            self.assertTrue((await r.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 204"))
            w.close()
        srv.close()
        self.assertIn(b"Host: " + long_name.encode() + b"\r\n", bytes(got))


if __name__ == "__main__":
    unittest.main()
