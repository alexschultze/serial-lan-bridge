"""Regenerate frames.json (run only when the wire format changes on purpose; bump PROTO_VERSION)."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tether-host"))
from tether_host import protocol as P  # noqa: E402

cases = [
    ("ping", P.Frame(P.T.PING, 0, bytes([1, 0, 0, 0]))),
    ("pong", P.Frame(P.T.PONG, 0, bytes([1, 0, 0, 0]))),
    ("hello_node", P.hello(P.Role.NODE, P.NodeState.NEEDS_CONFIG, 0x01020304, "tether-node 0.1")),
    ("accept", P.accept(1, 1, bytes([192, 168, 1, 7]), 51234)),
    ("credit", P.credit(1, 2048)),
    ("open", P.open_req(P.HOST_CHAN_BASE + 3, 7)),
    ("open_res_ok", P.open_res(P.HOST_CHAN_BASE + 3, P.Open.OK)),
    ("open_res_refused", P.open_res(P.HOST_CHAN_BASE + 16, P.Open.REFUSED)),
    ("close_eof", P.close(1, P.Close.EOF)),
    ("close_reset", P.close(16, P.Close.RESET)),
    ("data_small", P.Frame(P.T.DATA, 2, b"GET /v1/models HTTP/1.1\r\n\r\n")),
    ("data_zeros", P.Frame(P.T.DATA, 3, bytes(300))),
    ("data_all_bytes", P.Frame(P.T.DATA, 4, bytes(range(256)))),
    ("data_max", P.Frame(P.T.DATA, 5, bytes(range(256)) * 4)),
    ("config_more", P.Frame(P.T.CONFIG, 0, b'{"v":1,', P.FLAG_MORE)),
    ("config_last", P.Frame(P.T.CONFIG, 0, b'"x":2}')),
]
out = []
for name, f in cases:
    out.append(dict(name=name, type=int(f.type), flags=f.flags, chan=f.chan, payload=f.payload.hex(), wire=P.encode(f).hex()))
with open(os.path.join(os.path.dirname(__file__), "frames.json"), "w") as fh:
    json.dump(out, fh, indent=1)
