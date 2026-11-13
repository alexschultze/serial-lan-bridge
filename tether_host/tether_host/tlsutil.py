"""TLS towards the final server. The client side of a listener or forward speaks plain HTTP; tether-host (after rewriting the
Host header) talks HTTPS to the real server.

client_context(): the SSL context for that leg. TlsClient: a TLS client over in-memory buffers, for a forward, where the bytes
travel through the node and no socket is available on the PC."""
import functools
import logging
import ssl

log = logging.getLogger("tether.tls")


@functools.lru_cache(maxsize=2)
def client_context(verify=True):
    ctx = ssl.create_default_context()   # system certificate store (on Windows: the Windows store)
    ctx.set_alpn_protocols(["http/1.1"])   # the Host rewriter understands HTTP/1.x only
    if not verify:
        log.warning("certificate checking is switched off (tls_verify = false): the server's identity is not verified")
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class TlsClient:
    """feed() takes bytes from the server, encrypt() bytes from the client; both return what to send on the other side."""

    def __init__(self, ctx, server_name):
        self._in = ssl.MemoryBIO()
        self._out = ssl.MemoryBIO()
        self._obj = ctx.wrap_bio(self._in, self._out, server_hostname=server_name)   # name for SNI and certificate check
        self._pending = bytearray()   # client data that arrived before the handshake finished
        self.handshaken = False

    def start(self):
        """Begins the handshake; returns the first bytes for the server (ClientHello)."""
        self._handshake()
        return self._out.read()

    def _handshake(self):
        if not self.handshaken:
            try:
                self._obj.do_handshake()
                self.handshaken = True
            except ssl.SSLWantReadError:
                pass   # needs more bytes from the server; other SSLErrors (certificate rejected...) propagate

    def feed(self, data):
        """Returns (plain bytes for the client, bytes for the server, server closed)."""
        self._in.write(data)
        self._handshake()
        plain = bytearray()
        eof = False
        if self.handshaken:
            if self._pending:
                self._obj.write(bytes(self._pending))
                self._pending.clear()
            while True:
                try:
                    chunk = self._obj.read(65536)
                except ssl.SSLWantReadError:
                    break
                except (ssl.SSLZeroReturnError, ssl.SSLEOFError):   # close_notify, or closed without it (common)
                    eof = True
                    break
                if not chunk:   # the stream ended
                    eof = True
                    break
                plain += chunk
        return bytes(plain), self._out.read(), eof

    def encrypt(self, data):
        if not data:
            return b""
        if not self.handshaken:
            self._pending += data
            return b""
        self._obj.write(data)
        return self._out.read()

    def close(self):
        """The client is done: returns the closing alert for the server."""
        if self.handshaken:
            try:
                self._obj.unwrap()
            except ssl.SSLError:
                pass   # the server's own close_notify is not here yet; ours is already queued
        return self._out.read()
