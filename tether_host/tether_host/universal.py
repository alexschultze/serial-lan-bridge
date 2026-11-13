"""Universal forwarder: the destination of a connection is part of the URL, /<http|https>/<host>[:port]/<rest>.

For example http://127.0.0.1:9000/https/opencode.ai/v1/models goes to https://opencode.ai/v1/models with Host: opencode.ai.
parse() takes the request target of the first request of a connection; every later request on the same connection must use
the same prefix (HostRewriter(prefix=...) strips it and flags a mismatch)."""
import re
from dataclasses import dataclass

_PATH = re.compile(rb"^/(https?)/([A-Za-z0-9](?:[A-Za-z0-9._-]{0,61}[A-Za-z0-9])?)(?::([0-9]{1,5}))?(?=/|\?|$)")


@dataclass(frozen=True)
class Target:
    scheme: str     # "http" or "https"
    host: str
    port: int
    prefix: bytes   # the part of the request target that names this destination

    @property
    def tls(self):
        return self.scheme == "https"

    @property
    def host_value(self):
        """The Host header for this destination: the port is left out when it is the scheme's default."""
        return self.host if self.port == (443 if self.tls else 80) else f"{self.host}:{self.port}"


def parse(target: bytes):
    """Returns a Target for a request target such as b"/https/example.com:8443/a?b=1", or None if it does not name one."""
    m = _PATH.match(target)
    if not m:
        return None
    scheme = m.group(1).decode()
    port = int(m.group(3)) if m.group(3) else (443 if scheme == "https" else 80)
    if not 1 <= port <= 65535:
        return None
    return Target(scheme, m.group(2).decode().lower(), port, m.group(0))


def request_target(head: bytes):
    """The request target (second word of the request line) of an HTTP/1.x request head, or None."""
    line = head.split(b"\r\n", 1)[0].split(b" ")
    if len(line) != 3 or not line[2].startswith(b"HTTP/1."):
        return None
    return line[1]
