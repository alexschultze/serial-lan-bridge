"""Rewrites the Host header of HTTP/1.x requests in a byte stream (client -> server direction only).

A forward carries bytes, so a web server sees whatever Host the client sent ("127.0.0.1:9000"). Servers that host several
sites, or that guard against DNS rebinding (Cloudflare, FRITZ!Box), refuse such requests. HostRewriter replaces the Host
line of every request head with a fixed value and passes everything else through unchanged.

It follows the stream request by request (keep-alive), skipping bodies by Content-Length or chunked framing. It gives up
and passes the rest of the connection through untouched when it cannot follow: not HTTP, a head over 64 KB, a protocol
upgrade (WebSocket), CONNECT, or a malformed length."""

MAX_HEAD = 65536
MAX_LINE = 1024

HEAD, BODY, CHUNK_SIZE, CHUNK_DATA, TRAILER, RAW = range(6)


class HostRewriter:
    def __init__(self, host, prefix=None):
        self.host = host.encode("ascii") if isinstance(host, str) else bytes(host)
        self.prefix = prefix      # bytes: removed from the start of every request target (universal forwarder)
        self.bad = False          # a request did not carry the prefix: the caller must end the connection
        self.methods = None       # a deque, when a ResponseRewriter follows this connection: the method of every request
        self.orig_host = None     # the Host header the client sent in its first request (before it was replaced)
        self._in = bytearray()   # input not yet passed on (an incomplete head or line)
        self._state = HEAD
        self._left = 0           # bytes still to pass through in BODY / CHUNK_DATA
        self.rewritten = 0       # request heads whose Host line was replaced

    def feed(self, data):
        """Takes bytes from the client, returns the bytes to send on. May return less than given (a head is held back until
        it is complete) or more."""
        out = bytearray()
        self._in += data
        while self._in:
            st = self._state
            if st == RAW:
                out += self._in
                self._in.clear()
            elif st in (BODY, CHUNK_DATA):
                n = min(self._left, len(self._in))
                out += self._in[:n]
                del self._in[:n]
                self._left -= n
                if self._left == 0:
                    self._state = HEAD if st == BODY else CHUNK_SIZE
            elif st == HEAD:
                end = self._in.find(b"\r\n\r\n")
                if end < 0:
                    if len(self._in) > MAX_HEAD:
                        self._state = RAW
                        continue
                    break   # wait for the rest of the head
                head = bytes(self._in[:end + 4])
                del self._in[:end + 4]
                out += self._head(head)
            else:   # CHUNK_SIZE or TRAILER: one line at a time
                idx = self._in.find(b"\r\n")
                if idx < 0:
                    if len(self._in) > MAX_LINE:
                        self._state = RAW
                        continue
                    break
                line = bytes(self._in[:idx + 2])
                del self._in[:idx + 2]
                out += line
                if st == TRAILER:
                    if idx == 0:
                        self._state = HEAD
                else:
                    try:
                        size = int(line.split(b";", 1)[0].strip(), 16)
                    except ValueError:
                        self._state = RAW
                        continue
                    if size == 0:
                        self._state = TRAILER
                    else:
                        self._left = size + 2   # the chunk and its closing CRLF
                        self._state = CHUNK_DATA
        return bytes(out)

    def flush(self):
        """End of the client's data: whatever is held back (an incomplete head) goes on unchanged."""
        rest = bytes(self._in)
        self._in.clear()
        return rest

    def _head(self, head):
        lines = head[:-4].split(b"\r\n")
        if len(lines[0].split(b" ")) != 3 or not lines[0].rsplit(b" ", 1)[1].startswith(b"HTTP/1."):
            self._state = RAW   # not an HTTP/1.x request line
            return head
        method = lines[0].split(b" ", 1)[0].upper()
        if self.prefix:
            words = lines[0].split(b" ")
            t, p = words[1], self.prefix
            if t == p or (t.startswith(p) and t[len(p):len(p) + 1] in (b"/", b"?")):
                rest = t[len(p):]
                words[1] = b"/" + rest if rest[:1] != b"/" else rest   # "/x" stays, "" or "?q" becomes "/" or "/?q"
                lines[0] = b" ".join(words)
            else:
                self.bad = True
                self._state = RAW
                return b""
        length, chunked, upgrade, replaced = None, False, method == b"CONNECT", False
        for i in range(1, len(lines)):
            name, sep, value = lines[i].partition(b":")
            if not sep or name[:1] in (b" ", b"\t"):
                continue
            key = name.strip().lower()
            if key == b"host" and not replaced:
                if self.orig_host is None:
                    self.orig_host = value.strip().decode("latin-1")
                lines[i] = b"Host: " + self.host
                replaced = True
            elif key == b"content-length":
                try:
                    length = int(value.strip())
                except ValueError:
                    length = -1
                if length < 0:
                    self._state = RAW
                    return head
            elif key == b"transfer-encoding" and b"chunked" in value.lower():
                chunked = True
            elif key == b"upgrade":
                upgrade = True
        if replaced:
            self.rewritten += 1
        if self.methods is not None:
            self.methods.append(method)
        out = b"\r\n".join(lines) + b"\r\n\r\n"
        if upgrade:
            self._state = RAW
        elif chunked:
            self._state = CHUNK_SIZE
        elif length:
            self._state, self._left = BODY, length
        else:
            self._state = HEAD
        return out
