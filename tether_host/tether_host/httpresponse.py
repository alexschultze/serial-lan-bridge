"""Response side of the Host rewriting: redirects must stay inside the bridge.

A server that redirects (Location: https://opencode.ai/) sends the client straight out of the bridge. ResponseRewriter follows
the responses of one connection (like HostRewriter follows the requests) and replaces an absolute Location by the address of the
matching local entry: a forward or listener with that target, or a universal one (/<scheme>/<host>/<path>). Relative
Locations already stay on the same origin and are left alone. Only Location is touched; bodies, cookies and links are not."""
import collections
from urllib.parse import urlsplit

from .httprewrite import BODY, CHUNK_SIZE, HEAD, RAW, HostRewriter


class ResponseRewriter(HostRewriter):
    """feed() takes the bytes the server sent, returns the bytes for the client. `requests` is the HostRewriter of the same
    connection's request side (it records the method of every request, needed to know whether a response has a body)."""

    def __init__(self, requests, mapper):
        super().__init__(b"")
        if requests.methods is None:
            requests.methods = collections.deque()
        self.requests = requests
        self.mapper = mapper    # mapper(location, client_host) -> (new location, close the connection) or None
        self.redirected = 0     # Location headers that were replaced

    def _head(self, head):
        lines = head[:-4].split(b"\r\n")
        words = lines[0].split(b" ", 2)
        if len(words) < 2 or not words[0].startswith(b"HTTP/1.") or not words[1].isdigit():
            self._state = RAW   # not an HTTP/1.x status line
            return head
        status = int(words[1])
        if 100 <= status < 200 and status != 101:   # an interim answer (100 Continue): the real one follows
            self._state = HEAD
            return head
        method = self.requests.methods.popleft() if self.requests.methods else b"GET"
        length, chunked, loc = None, False, None
        for i in range(1, len(lines)):
            name, sep, value = lines[i].partition(b":")
            if not sep or name[:1] in (b" ", b"\t"):
                continue
            key = name.strip().lower()
            if key == b"content-length":
                try:
                    length = int(value.strip())
                except ValueError:
                    self._state = RAW
                    return head
            elif key == b"transfer-encoding" and b"chunked" in value.lower():
                chunked = True
            elif key == b"location" and loc is None:
                loc = i
        if status == 101 or (method == b"CONNECT" and 200 <= status < 300):
            self._state = RAW   # the connection is no longer HTTP
        elif method == b"HEAD" or status in (204, 304):
            self._state = HEAD  # no body, whatever the headers claim
        elif chunked:
            self._state = CHUNK_SIZE
        elif length is not None:
            self._state, self._left = (BODY, length) if length else (HEAD, 0)
        else:
            self._state = RAW   # the body runs until the server closes the connection
        if loc is not None and self.requests.orig_host:
            try:
                res = self.mapper(lines[loc].partition(b":")[2].strip().decode("latin-1"), self.requests.orig_host)
            except ValueError:
                res = None
            if res:
                new, close = res
                lines[loc] = b"Location: " + new.encode("latin-1")
                self.redirected += 1
                if close:   # the next request names another destination: it needs a new connection
                    lines = [l for l in lines if l.partition(b":")[0].strip().lower() != b"connection"]
                    lines.append(b"Connection: close")
        return b"\r\n".join(lines) + b"\r\n\r\n"


def _client_hostname(client_host):
    """'127.0.0.1:9080' -> '127.0.0.1', '[::1]:9000' -> '[::1]'."""
    if client_host.startswith("["):
        return client_host[:client_host.index("]") + 1]
    return client_host.split(":")[0]


def make_mapper(cfg, kind, entry, dest=None):
    """The Location mapper of one connection. kind: 'forward' or 'listener' (which local entries the client can reach);
    entry: the entry the connection came in on; dest: (scheme, host, port) of a universal connection."""
    entries = cfg.forwards if kind == "forward" else cfg.listeners

    def local_port(e):
        return e.listen_port if kind == "forward" else e.port

    def mapper(location, client_host):
        u = urlsplit(location)
        if u.scheme not in ("http", "https") or not u.hostname:
            return None   # relative: stays on the same origin
        scheme = u.scheme
        host = u.hostname.lower()
        port = u.port or (443 if scheme == "https" else 80)
        base = _client_hostname(client_host)
        rest = (u.path or "/") + ("?" + u.query if u.query else "") + ("#" + u.fragment if u.fragment else "")
        for e in entries:   # a fixed entry for exactly this destination
            if (not e.universal and e.target_host.lower() == host and e.target_port == port
                    and (e.tls == "connect") == (scheme == "https")):
                return f"http://{base}:{local_port(e)}{rest}", False
        netloc = u.netloc.rsplit("@", 1)[-1]
        universal = [e for e in entries if e.universal]
        universal.sort(key=lambda e: e is not entry)   # this connection's own entry first
        if universal:
            e = universal[0]
            same_dest = e is entry and dest == (scheme, host, port)
            return f"http://{base}:{local_port(e)}/{scheme}/{netloc}{rest}", e is entry and not same_dest
        return None

    return mapper
