"""Universal forwarder, redirect mapping (response rewriting) and HTTPS on the final leg."""
import asyncio
import datetime
import ipaddress
import os
import socket
import ssl
import tempfile
import threading
import unittest

from tether_host import universal as U
from tether_host.config import ConfigError, parse_config
from tether_host.httprewrite import HostRewriter
from tether_host.httpresponse import ResponseRewriter, make_mapper
from tether_host.tlsutil import TlsClient
from test_e2e import Rig, echo_server, free_port

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    HAVE_CRYPTO = True
except ImportError:   # the TLS tests need it to make a throwaway certificate
    HAVE_CRYPTO = False

WIFI = {"network": [{"ssid": "x", "psk": "password1"}]}


def cfg_of(**sections):
    sections.setdefault("host", {"connect_timeout": 2})
    return parse_config({"wifi": WIFI, **sections})


def feed_all(rw, data, step):
    return b"".join(rw.feed(data[i:i + step]) for i in range(0, len(data), step)) + rw.flush()


# ---- destination in the URL ------------------------------------------------------------------------

class ParseTests(unittest.TestCase):
    def test_destinations(self):
        t = U.parse(b"/https/opencode.ai/v1/models")
        self.assertEqual((t.scheme, t.host, t.port, t.prefix, t.host_value),
                         ("https", "opencode.ai", 443, b"/https/opencode.ai", "opencode.ai"))
        t = U.parse(b"/http/Example.com:8080?x=1")
        self.assertEqual((t.host, t.port, t.host_value), ("example.com", 8080, "example.com:8080"))
        self.assertEqual(U.parse(b"/http/a").port, 80)
        self.assertTrue(U.parse(b"/https/a").tls)

    def test_not_a_destination(self):
        for bad in (b"/", b"/ftp/x/", b"/https//x", b"/https/-x/", b"/https/a:0/", b"/https/a:70000/", b"/httpsx/a/", b"https/a/"):
            self.assertIsNone(U.parse(bad), bad)

    def test_request_target(self):
        self.assertEqual(U.request_target(b"GET /a/b HTTP/1.1\r\nHost: h\r\n\r\n"), b"/a/b")
        self.assertIsNone(U.request_target(b"\x16\x03\x01 binary"))


class PrefixTests(unittest.TestCase):
    def test_prefix_is_stripped_from_every_request(self):
        t = U.parse(b"/https/opencode.ai/")
        rw = HostRewriter(t.host_value, t.prefix)
        out = rw.feed(b"GET /https/opencode.ai/v1/x?a=1 HTTP/1.1\r\nHost: 127.0.0.1:9000\r\n\r\n"
                      b"GET /https/opencode.ai HTTP/1.1\r\nHost: x\r\n\r\n"
                      b"GET /https/opencode.ai?q HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertEqual(out, b"GET /v1/x?a=1 HTTP/1.1\r\nHost: opencode.ai\r\n\r\n"
                              b"GET / HTTP/1.1\r\nHost: opencode.ai\r\n\r\n"
                              b"GET /?q HTTP/1.1\r\nHost: opencode.ai\r\n\r\n")
        self.assertFalse(rw.bad)

    def test_another_destination_on_the_connection_is_flagged(self):
        rw = HostRewriter("h", b"/http/a")
        self.assertEqual(rw.feed(b"GET /http/b/x HTTP/1.1\r\nHost: x\r\n\r\n"), b"")
        self.assertTrue(rw.bad)
        rw = HostRewriter("h", b"/http/a")
        rw.feed(b"GET /http/ab/x HTTP/1.1\r\nHost: x\r\n\r\n")   # "/http/ab" is not "/http/a" plus a path
        self.assertTrue(rw.bad)


# ---- redirects stay inside the bridge ----------------------------------------------------------------

class RedirectTests(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg_of(forward=[
            {"id": 1, "listen": 9080, "target": "opencode.ai:80", "host_header": "auto"},
            {"id": 2, "listen": 9443, "target": "opencode.ai:443", "host_header": "auto", "tls": "connect"},
            {"id": 4, "listen": 9000, "universal": True}])

    def responses(self, entry, request, response, dest=None, step=1000):
        rq = HostRewriter("h", U.parse(request.split(b" ")[1]).prefix if dest else None)
        rs = ResponseRewriter(rq, make_mapper(self.cfg, "forward", entry, dest))   # first: it records the request methods
        rq.feed(request)
        return feed_all(rs, response, step)

    def test_redirect_to_https_goes_to_the_https_forward(self):
        out = self.responses(self.cfg.forwards[0], b"GET / HTTP/1.1\r\nHost: 127.0.0.1:9080\r\n\r\n",
                             b"HTTP/1.1 301 Moved\r\nLocation: https://opencode.ai/x?y=1\r\nContent-Length: 0\r\n\r\n")
        self.assertIn(b"Location: http://127.0.0.1:9443/x?y=1\r\n", out)

    def test_name_the_client_used_is_kept(self):
        out = self.responses(self.cfg.forwards[0], b"GET / HTTP/1.1\r\nHost: tether-327c:9080\r\n\r\n",
                             b"HTTP/1.1 301 Moved\r\nLocation: https://opencode.ai/\r\nContent-Length: 0\r\n\r\n")
        self.assertIn(b"Location: http://tether-327c:9443/\r\n", out)

    def test_relative_and_foreign_redirects_are_left_alone(self):
        for loc in (b"/login", b"login", b"//cdn.example.org/x", b"mailto:a@b"):
            resp = b"HTTP/1.1 302 Found\r\nLocation: " + loc + b"\r\nContent-Length: 0\r\n\r\n"
            out = self.responses(self.cfg.forwards[1], b"GET / HTTP/1.1\r\nHost: 127.0.0.1:9443\r\n\r\n", resp)
            self.assertIn(b"Location: " + loc + b"\r\n", out, loc)

    def test_universal_prefixes_the_destination_and_closes_when_it_changes(self):
        uni = self.cfg.forwards[2]
        dest = ("http", "other.example", 80)
        req = b"GET /http/other.example/a HTTP/1.1\r\nHost: 127.0.0.1:9000\r\n\r\n"
        out = self.responses(uni, req, b"HTTP/1.1 301 M\r\nConnection: keep-alive\r\nLocation: https://other.example/b\r\n"
                                       b"Content-Length: 0\r\n\r\n", dest)
        self.assertIn(b"Location: http://127.0.0.1:9000/https/other.example/b\r\n", out)
        self.assertIn(b"Connection: close\r\n", out)
        self.assertNotIn(b"keep-alive", out)
        # the same destination again: the connection may stay
        out = self.responses(uni, req, b"HTTP/1.1 302 F\r\nLocation: http://other.example/c\r\nContent-Length: 0\r\n\r\n", dest)
        self.assertIn(b"Location: http://127.0.0.1:9000/http/other.example/c\r\n", out)
        self.assertNotIn(b"Connection: close", out)

    def test_an_exact_forward_is_preferred_over_the_universal_one(self):
        uni = self.cfg.forwards[2]
        req = b"GET /http/opencode.ai/a HTTP/1.1\r\nHost: 127.0.0.1:9000\r\n\r\n"
        out = self.responses(uni, req, b"HTTP/1.1 301 M\r\nLocation: https://opencode.ai/b\r\nContent-Length: 0\r\n\r\n",
                             ("http", "opencode.ai", 80))
        self.assertIn(b"Location: http://127.0.0.1:9443/b\r\n", out)

    def test_bodies_are_skipped_so_the_next_response_is_found(self):
        reqs = (b"GET / HTTP/1.1\r\nHost: a:9080\r\n\r\n" b"HEAD / HTTP/1.1\r\nHost: a:9080\r\n\r\n"
                b"GET / HTTP/1.1\r\nHost: a:9080\r\n\r\n" b"GET / HTTP/1.1\r\nHost: a:9080\r\n\r\n")
        fake_head = b"HTTP/1.1 301 X\r\nLocation: https://opencode.ai/inside\r\n\r\n"
        resp = (b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(fake_head) + fake_head +   # looks like a head, is a body
                b"HTTP/1.1 200 OK\r\nContent-Length: 999\r\n\r\n"                                 # answer to HEAD: no body
                b"HTTP/1.1 204 No Content\r\n\r\n"
                b"HTTP/1.1 301 Moved\r\nTransfer-Encoding: chunked\r\nLocation: https://opencode.ai/end\r\n\r\n"
                b"5\r\nhello\r\n0\r\n\r\n")
        for step in (1, 7, 1000):
            rq = HostRewriter("h")
            rs = ResponseRewriter(rq, make_mapper(self.cfg, "forward", self.cfg.forwards[0]))   # before the requests
            rq.feed(reqs)
            out = feed_all(rs, resp, step)
            self.assertIn(b"Location: https://opencode.ai/inside\r\n", out, step)   # inside a body: untouched
            self.assertIn(b"Location: http://a:9443/end\r\n", out, step)            # the real redirect: mapped

    def test_interim_100_continue_does_not_consume_a_request(self):
        rq = HostRewriter("h")
        rs = ResponseRewriter(rq, make_mapper(self.cfg, "forward", self.cfg.forwards[0]))
        rq.feed(b"POST / HTTP/1.1\r\nHost: a:9080\r\nContent-Length: 0\r\n\r\n")
        out = rs.feed(b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 301 M\r\nLocation: https://opencode.ai/z\r\nContent-Length: 0\r\n\r\n")
        self.assertIn(b"Location: http://a:9443/z\r\n", out)

    def test_upgrade_and_until_close_bodies_pass_through(self):
        rq = HostRewriter("h")
        rs = ResponseRewriter(rq, make_mapper(self.cfg, "forward", self.cfg.forwards[0]))
        rq.feed(b"GET / HTTP/1.1\r\nHost: a:9080\r\nUpgrade: websocket\r\n\r\n")
        data = b"HTTP/1.1 101 Switching\r\nUpgrade: websocket\r\n\r\n\x81\x03abc"
        self.assertEqual(rs.feed(data), data)


# ---- configuration -----------------------------------------------------------------------------------

class ConfigTests(unittest.TestCase):
    def test_universal_forward_and_listener(self):
        c = cfg_of(forward=[{"id": 1, "listen": 9000, "universal": True}], listener=[{"id": 1, "port": 8000, "universal": True}])
        self.assertTrue(c.forwards[0].universal and c.listeners[0].universal)
        self.assertEqual(c.forwards[0].label, "any host (universal)")
        self.assertIn(b'"forwards":[{"id":1,"any":true}]', c.node_json())

    def test_universal_excludes_target_host_header_tls_and_tcp(self):
        for extra in ({"target": "a:80"}, {"host_header": "auto"}, {"tls": "connect"}, {"protocol": "tcp"}):
            with self.assertRaises(ConfigError, msg=str(extra)):
                cfg_of(forward=[{"id": 1, "listen": 9000, "universal": True, **extra}])

    def test_tls_options(self):
        c = cfg_of(forward=[{"id": 1, "listen": 9000, "target": "a:443", "tls": "connect", "tls_verify": False}])
        self.assertEqual((c.forwards[0].tls, c.forwards[0].tls_verify), ("connect", False))
        c = cfg_of(listener=[{"id": 1, "port": 9000, "target": "a:443", "tls": "connect"}])
        self.assertEqual((c.listeners[0].tls, c.listeners[0].tls_verify), ("connect", True))
        for bad in ({"tls": "yes"}, {"tls_verify": "no"}):
            with self.assertRaises(ConfigError, msg=str(bad)):
                cfg_of(forward=[{"id": 1, "listen": 9000, "target": "a:443", **bad}])

    def test_open_request_carries_the_endpoint_only_when_asked(self):
        from tether_host import protocol as P
        self.assertEqual(len(P.open_req(0x8001, 4).payload), 2)
        f = P.open_req(0x8001, 4, "opencode.ai", 443)
        self.assertEqual(f.payload, (4).to_bytes(2, "little") + (443).to_bytes(2, "little") + b"opencode.ai")
        for bad in (("", 443), ("a" * 64, 443), ("a", 0)):
            with self.assertRaises(P.FrameError):
                P.open_req(0x8001, 4, *bad)


# ---- end to end through the bridge --------------------------------------------------------------------

async def http_get(port, request):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(request)
    head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 10)
    w.close()
    return head


def recording_server(heads, reply=b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"):
    async def handler(r, w):
        heads.append(await r.readuntil(b"\r\n\r\n"))
        w.write(reply() if callable(reply) else reply)
        await w.drain()
        w.close()
    return handler


class UniversalE2E(unittest.IsolatedAsyncioTestCase):
    async def test_universal_forward_strips_the_prefix_and_names_the_host(self):
        heads = []
        srv, tp = await echo_server(recording_server(heads))
        fp = free_port()
        cfg = cfg_of(forward=[{"id": 1, "listen": fp, "universal": True}])
        async with Rig(cfg) as rig:
            self.assertEqual(rig.node.received_config["forwards"], [{"id": 1, "any": True}])
            head = await http_get(fp, b"GET /http/127.0.0.1:%d/hello?q=1 HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % (tp, fp))
            self.assertTrue(head.startswith(b"HTTP/1.1 200"))
        srv.close()
        self.assertTrue(heads[0].startswith(b"GET /hello?q=1 HTTP/1.1\r\n"), heads[0])
        self.assertIn(b"Host: 127.0.0.1:%d\r\n" % tp, heads[0])

    async def test_universal_forward_answers_400_without_a_destination(self):
        fp = free_port()
        async with Rig(cfg_of(forward=[{"id": 1, "listen": fp, "universal": True}])):
            head = await http_get(fp, b"GET /nothing HTTP/1.1\r\nHost: x\r\n\r\n")
            self.assertTrue(head.startswith(b"HTTP/1.1 400"))

    async def test_universal_forward_reports_an_unreachable_destination(self):
        fp, dead = free_port(), free_port()
        async with Rig(cfg_of(forward=[{"id": 1, "listen": fp, "universal": True}])):
            head = await http_get(fp, b"GET /http/127.0.0.1:%d/ HTTP/1.1\r\nHost: x\r\n\r\n" % dead)
            self.assertTrue(head.startswith((b"HTTP/1.1 502", b"HTTP/1.1 504")), head)

    async def test_universal_listener(self):
        heads = []
        srv, tp = await echo_server(recording_server(heads))
        lp = free_port()
        cfg = cfg_of(listener=[{"id": 1, "port": lp, "universal": True}])
        async with Rig(cfg):
            head = await http_get(lp, b"GET /http/127.0.0.1:%d/a/b HTTP/1.1\r\nHost: tether-xxxx:%d\r\n\r\n" % (tp, lp))
            self.assertTrue(head.startswith(b"HTTP/1.1 200"))
            head = await http_get(lp, b"GET /nope HTTP/1.1\r\nHost: x\r\n\r\n")
            self.assertTrue(head.startswith(b"HTTP/1.1 400"))
        srv.close()
        self.assertTrue(heads[0].startswith(b"GET /a/b HTTP/1.1\r\n"), heads[0])
        self.assertIn(b"Host: 127.0.0.1:%d\r\n" % tp, heads[0])

    async def test_redirect_is_mapped_onto_the_other_forward(self):
        heads_a, heads_b = [], []
        srv_b, tb = await echo_server(recording_server(heads_b))
        srv_a, ta = await echo_server(recording_server(
            heads_a, lambda: b"HTTP/1.1 301 Moved\r\nLocation: http://127.0.0.1:%d/x?y=1\r\nContent-Length: 0\r\n\r\n" % tb))
        fa, fb = free_port(), free_port()
        cfg = cfg_of(forward=[{"id": 1, "listen": fa, "target": "127.0.0.1:%d" % ta, "host_header": "auto"},
                              {"id": 2, "listen": fb, "target": "127.0.0.1:%d" % tb, "host_header": "auto"}])
        async with Rig(cfg):
            head = await http_get(fa, b"GET / HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % fa)
        srv_a.close()
        srv_b.close()
        self.assertIn(b"Location: http://127.0.0.1:%d/x?y=1\r\n" % fb, head)


# ---- HTTPS on the final leg -----------------------------------------------------------------------------

def make_cert(directory):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                                                        x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = os.path.join(directory, "c.pem"), os.path.join(directory, "k.pem")
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    return cert_path, key_path


@unittest.skipUnless(HAVE_CRYPTO, "the cryptography package makes the test certificate")
class TlsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cert, self.key = make_cert(self.dir.name)
        self.server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.server_ctx.load_cert_chain(self.cert, self.key)

    def tearDown(self):
        self.dir.cleanup()

    def tls_thread_server(self, reply):
        """A blocking TLS server in a thread; returns (port, list of request heads)."""
        lsock = socket.socket()
        lsock.bind(("127.0.0.1", 0))
        lsock.listen(1)
        heads = []

        def run():
            conn, _ = lsock.accept()
            try:
                with self.server_ctx.wrap_socket(conn, server_side=True) as s:
                    buf = b""
                    while b"\r\n\r\n" not in buf:
                        buf += s.recv(4096)
                    heads.append(buf)
                    s.sendall(reply)
            except (ssl.SSLError, OSError):
                pass
            finally:
                lsock.close()
        threading.Thread(target=run, daemon=True).start()
        return lsock.getsockname()[1], heads

    def roundtrip(self, port, ctx, request):
        tls = TlsClient(ctx, "localhost")
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            sock.sendall(tls.start())
            sock.sendall(tls.encrypt(request))   # queued until the handshake is done
            body = b""
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                plain, out, eof = tls.feed(data)
                if out:
                    sock.sendall(out)
                body += plain
                if eof:
                    break
        return body

    def test_tls_client_talks_to_a_tls_server(self):
        port, heads = self.tls_thread_server(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        body = self.roundtrip(port, ssl.create_default_context(cafile=self.cert), b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        self.assertTrue(body.endswith(b"ok"), body)
        self.assertTrue(heads[0].startswith(b"GET / HTTP/1.1"))

    def test_tls_client_rejects_an_untrusted_certificate(self):
        port, _ = self.tls_thread_server(b"HTTP/1.1 200 OK\r\n\r\n")
        with self.assertRaises(ssl.SSLError):
            self.roundtrip(port, ssl.create_default_context(), b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")

    async def tls_server(self, heads):
        async def handler(r, w):
            heads.append(await r.readuntil(b"\r\n\r\n"))
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            await w.drain()
            w.close()
        srv = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=self.server_ctx)
        return srv, srv.sockets[0].getsockname()[1]

    async def test_forward_with_tls_connect(self):
        heads = []
        srv, tp = await self.tls_server(heads)
        fp = free_port()
        cfg = cfg_of(forward=[{"id": 1, "listen": fp, "target": "127.0.0.1:%d" % tp, "host_header": "auto",
                               "tls": "connect", "tls_verify": False}])
        async with Rig(cfg):
            head = await http_get(fp, b"GET /p HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % fp)
            self.assertTrue(head.startswith(b"HTTP/1.1 200"), head)
        srv.close()
        self.assertIn(b"Host: 127.0.0.1:%d\r\n" % tp, heads[0])

    async def test_forward_with_a_rejected_certificate_answers_502(self):
        heads = []
        srv, tp = await self.tls_server(heads)
        fp = free_port()
        cfg = cfg_of(forward=[{"id": 1, "listen": fp, "target": "127.0.0.1:%d" % tp, "tls": "connect"}])   # verify on
        async with Rig(cfg):
            head = await http_get(fp, b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            self.assertTrue(head.startswith(b"HTTP/1.1 502"), head)
        srv.close()

    async def test_listener_with_tls_connect(self):
        heads = []
        srv, tp = await self.tls_server(heads)
        lp = free_port()
        cfg = cfg_of(listener=[{"id": 1, "port": lp, "target": "127.0.0.1:%d" % tp, "tls": "connect", "tls_verify": False,
                                "host_header": "auto"}])
        async with Rig(cfg):
            head = await http_get(lp, b"GET / HTTP/1.1\r\nHost: tether:%d\r\n\r\n" % lp)
            self.assertTrue(head.startswith(b"HTTP/1.1 200"), head)
        srv.close()
        self.assertIn(b"Host: 127.0.0.1:%d\r\n" % tp, heads[0])

    async def test_universal_forward_to_an_https_destination(self):
        heads = []
        srv, tp = await self.tls_server(heads)
        fp = free_port()
        cfg = cfg_of(forward=[{"id": 1, "listen": fp, "universal": True, "tls_verify": False}])
        async with Rig(cfg):
            head = await http_get(fp, b"GET /https/127.0.0.1:%d/secure HTTP/1.1\r\nHost: x\r\n\r\n" % tp)
            self.assertTrue(head.startswith(b"HTTP/1.1 200"), head)
        srv.close()
        self.assertTrue(heads[0].startswith(b"GET /secure HTTP/1.1\r\n"), heads[0])


if __name__ == "__main__":
    unittest.main()
