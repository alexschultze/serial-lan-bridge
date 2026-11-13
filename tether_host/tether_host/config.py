"""Load and validate tether.toml; build the node-facing CONFIG JSON."""
import ipaddress
import json
import os
import sys
import tomllib
from dataclasses import dataclass, field

MAX_NETWORKS = 4
MAX_LISTENERS = 4
MAX_FORWARDS = 8


class ConfigError(Exception):
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclass
class HostSettings:
    port: str = "auto"
    serial: str = ""
    log_level: str = "info"
    connect_timeout: float = 5.0


@dataclass
class LinkSettings:
    window: int = 4096
    never_used_timeout: int = 30
    idle_timeout: int = 0


@dataclass
class Network:
    ssid: str
    psk: str = ""
    hidden: bool = False


@dataclass
class Hotspot:
    mode: str = "auto"
    ssid: str = ""
    psk: str = ""
    fallback: int = 30


def _host_value(header, host, port):
    """Host header value for `host_header` of a listener or forward: None = leave requests alone, "auto" = the target's name."""
    if not header:
        return None
    if header == "auto":
        return host if port in (80, 443) else f"{host}:{port}"
    return header


def _check_host_header(p, where, value, proto):
    """Validates a host_header setting; returns the value to keep ("" when it was rejected)."""
    if not isinstance(value, str) or len(value) > 255 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        p.append(f"{where}.host_header must be \"auto\" or a host name (printable ASCII, no spaces), at most 255 characters")
        return ""
    if value and proto != "http":
        p.append(f"{where}.host_header needs protocol = \"http\"")
    return value


def _check_universal(p, where, f, proto):
    """Reads `universal`; a universal listener or forward has no target and sets the Host header and TLS from the URL itself."""
    uni = f.get("universal", False)
    if not isinstance(uni, bool):
        p.append(f"{where}.universal must be true or false")
        return False
    if uni:
        for key in ("target", "host_header", "tls"):
            if key in f:
                p.append(f"{where}.{key} must be left out when universal = true (the URL decides)")
        if proto != "http":
            p.append(f"{where}.universal needs protocol = \"http\"")
    return uni


def _check_tls(p, where, f):
    """Reads `tls` and `tls_verify` of a listener or forward; returns (tls, verify)."""
    tls = f.get("tls", "")
    verify = f.get("tls_verify", True)
    if tls not in ("", "connect"):
        p.append(f"{where}.tls must be \"connect\" or left out")
        tls = ""
    if not isinstance(verify, bool):
        p.append(f"{where}.tls_verify must be true or false")
        verify = True
    return tls, verify


@dataclass
class Listener:
    id: int
    port: int
    target_host: str
    target_port: int
    protocol: str = "http"
    allow: list = field(default_factory=list)
    host_header: str = ""   # "": requests pass unchanged; "auto": the target's name; anything else: that value (PC side only)
    tls: str = ""           # "connect": the PC speaks HTTPS to the target while the client side stays plain
    tls_verify: bool = True # check the target's certificate (false: accept any, e.g. a device with a self-signed one)
    universal: bool = False # the destination comes from the URL: /<http|https>/<host>[:port]/<rest> (no target)

    @property
    def http(self):
        return self.protocol == "http"

    @property
    def host_value(self):
        """The Host header value to put into every request, or None to leave requests unchanged."""
        return _host_value(self.host_header, self.target_host, self.target_port)


@dataclass
class Forward:
    """A port on the PC (normally loopback) carried to an endpoint on the node's Wi-Fi network."""
    id: int
    listen_host: str
    listen_port: int
    target_host: str      # IPv4 address or a name the node resolves on its own network
    target_port: int
    protocol: str = "http"
    connect_timeout: float = 5.0
    host_header: str = ""   # "": requests pass unchanged; "auto": the target's name; anything else: that value (PC side only)
    tls: str = ""           # "connect": the PC speaks HTTPS to the target while the client side stays plain
    tls_verify: bool = True # check the target's certificate (false: accept any, e.g. a device with a self-signed one)
    universal: bool = False # the destination comes from the URL: /<http|https>/<host>[:port]/<rest> (no target)

    @property
    def http(self):
        return self.protocol == "http"

    @property
    def host_value(self):
        """The Host header value to put into every request, or None to leave requests unchanged."""
        return _host_value(self.host_header, self.target_host, self.target_port)

    @property
    def label(self):
        """The target as shown in the log."""
        return "any host (universal)" if self.universal else f"{self.target_host}:{self.target_port}"

    @property
    def exposed(self):
        """True when the local port is reachable from other machines."""
        return self.listen_host not in ("127.0.0.1", "localhost", "::1") and not self.listen_host.startswith("127.")


@dataclass
class Config:
    host: HostSettings
    link: LinkSettings
    country: str
    networks: list
    hotspot: Hotspot | None
    listeners: list
    forwards: list = field(default_factory=list)

    def listener(self, lid):
        for l in self.listeners:
            if l.id == lid:
                return l
        return None

    def forward(self, fid):
        for f in self.forwards:
            if f.id == fid:
                return f
        return None

    def node_json(self) -> bytes:
        """The configuration as sent to the node. Host-only settings and listener targets are left out."""
        wifi = {"country": self.country, "networks": []}
        for n in self.networks:
            e = {"ssid": n.ssid, "hidden": n.hidden}
            if n.psk:
                e["psk"] = n.psk
            wifi["networks"].append(e)
        doc = {
            "v": 1,
            "wifi": wifi,
            "hotspot": None if self.hotspot is None else {
                "mode": self.hotspot.mode, "ssid": self.hotspot.ssid,
                "psk": self.hotspot.psk, "fallback": self.hotspot.fallback},
            "listeners": [{"id": l.id, "port": l.port, "http": l.http, "allow": l.allow} for l in self.listeners],
            "forwards": [{"id": f.id, "any": True} if f.universal else
                         {"id": f.id, "host": f.target_host, "port": f.target_port} for f in self.forwards],
            "link": {"window": self.link.window, "never_used_timeout": self.link.never_used_timeout,
                     "idle_timeout": self.link.idle_timeout},
        }
        return json.dumps(doc, separators=(",", ":")).encode()


def default_paths():
    paths = [os.path.join(os.getcwd(), "tether.toml")]
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        if base:
            paths.append(os.path.join(base, "tether", "tether.toml"))
    else:
        paths.append(os.path.join(os.path.expanduser("~"), ".config", "tether", "tether.toml"))
    return paths


def find_config(explicit=None):
    if explicit:
        return explicit
    for p in default_paths():
        if os.path.isfile(p):
            return p
    raise ConfigError(["no configuration file found; tried: " + ", ".join(default_paths())])


def state_dir():
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "tether")
    return os.path.join(os.path.expanduser("~"), ".local", "state", "tether")


def load_config(path) -> Config:
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except OSError as e:
        raise ConfigError([f"cannot read {path}: {e}"])
    except tomllib.TOMLDecodeError as e:
        raise ConfigError([f"{path}: {e}"])
    return parse_config(raw)


def _int(problems, where, v, lo, hi):
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        problems.append(f"{where} must be an integer {lo}..{hi}")
        return lo
    return v


def _hostport(problems, where, v, default_host=None):
    """'host:port', or just a port (or integer) when a default host is allowed."""
    text = str(v) if v is not None else ""
    host, sep, port = text.rpartition(":")
    if not sep:
        host, port = default_host or "", text
    try:
        n = int(port)
        assert host and 1 <= n <= 65535
    except (ValueError, AssertionError):
        problems.append(f"{where} must be host:port" + (" or a port number" if default_host else ""))
        return "", 0
    return host, n


# The exact placeholder texts of tether.example.toml (a test keeps this list and the template in sync). Only these count:
# real values such as a password may contain < and > themselves.
TEMPLATE_PLACEHOLDERS = (
    "<Wi-Fi name>",
    "<Wi-Fi password>",
    "<hotspot password, 8..63 characters>",
    "<API host or address on the PC's network>",
    "<device address or name on the Wi-Fi network>",
)


def _placeholders(problems, value, where=""):
    """Report every value still holding one of the template's placeholders."""
    if isinstance(value, dict):
        for k, v in value.items():
            _placeholders(problems, v, f"{where}.{k}" if where else k)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _placeholders(problems, v, f"{where}[{i}]")
    elif isinstance(value, str) and any(ph in value for ph in TEMPLATE_PLACEHOLDERS):
        problems.append(f"{where} still holds the template placeholder {value!r}: replace it with your value")


def parse_config(raw: dict) -> Config:
    p = []
    _placeholders(p, raw)
    h = raw.get("host", {})
    ct = h.get("connect_timeout", 5)
    if isinstance(ct, bool) or not isinstance(ct, (int, float)):
        p.append("host.connect_timeout must be a number of seconds")
        ct = 5
    host = HostSettings(
        port=str(h.get("port", "auto")), serial=str(h.get("serial", "")),
        log_level=str(h.get("log_level", "info")).lower(), connect_timeout=float(ct))
    if host.log_level not in ("debug", "info", "warning", "error"):
        p.append("host.log_level must be debug, info, warning or error")
    if not 0.1 <= host.connect_timeout <= 120:
        p.append("host.connect_timeout must be 0.1..120 seconds")

    lk = raw.get("link", {})
    link = LinkSettings(
        window=_int(p, "link.window", lk.get("window", 4096), 1024, 16384),
        never_used_timeout=_int(p, "link.never_used_timeout", lk.get("never_used_timeout", 30), 0, 3600),
        idle_timeout=_int(p, "link.idle_timeout", lk.get("idle_timeout", 0), 0, 86400))

    w = raw.get("wifi", {})
    country = str(w.get("country", "DE"))
    if len(country) != 2 or not country.isalpha():
        p.append("wifi.country must be a two-letter code")
    networks = []
    for i, n in enumerate(w.get("network", [])):
        ssid, psk = n.get("ssid", ""), n.get("psk", "")
        if not isinstance(ssid, str) or not 1 <= len(ssid.encode()) <= 32:
            p.append(f"wifi.network[{i}].ssid must be 1..32 bytes")
        if psk and not 8 <= len(psk) <= 63:
            p.append(f"wifi.network[{i}].psk must be 8..63 characters (or omitted for an open network)")
        networks.append(Network(ssid, psk, bool(n.get("hidden", False))))
    if not networks:
        p.append("at least one [[wifi.network]] is required")
    if len(networks) > MAX_NETWORKS:
        p.append(f"at most {MAX_NETWORKS} networks")

    hotspot = None
    if "hotspot" in raw:
        hs = raw["hotspot"]
        hotspot = Hotspot(str(hs.get("mode", "auto")), str(hs.get("ssid", "")), str(hs.get("psk", "")),
                          _int(p, "hotspot.fallback", hs.get("fallback", 30), 0, 3600))
        if hotspot.mode not in ("auto", "always", "never"):
            p.append("hotspot.mode must be auto, always or never")
        if len(hotspot.ssid.encode()) > 32:
            p.append("hotspot.ssid must be at most 32 bytes")
        if not 8 <= len(hotspot.psk) <= 63:
            p.append("hotspot.psk is required, 8..63 characters (the hotspot is never open)")
        if hotspot.mode == "never":
            hotspot = None

    listeners = []
    ids, ports = set(), set()
    for i, l in enumerate(raw.get("listener", [])):
        where = f"listener[{i}]"
        lid = _int(p, where + ".id", l.get("id"), 1, 65535)
        port = _int(p, where + ".port", l.get("port"), 1, 65535)
        if lid in ids:
            p.append(f"{where}.id {lid} is used twice")
        if port in ports:
            p.append(f"{where}.port {port} is used twice")
        ids.add(lid)
        ports.add(port)
        proto = str(l.get("protocol", "http"))
        if proto not in ("http", "tcp"):
            p.append(f"{where}.protocol must be http or tcp")
        uni = _check_universal(p, where, l, proto)
        if uni:
            th, tport = "universal", 0
        else:
            target = str(l.get("target", ""))
            th, _, tp = target.rpartition(":")
            try:
                tport = int(tp)
                assert th and 1 <= tport <= 65535
            except (ValueError, AssertionError):
                p.append(f"{where}.target must be host:port")
                th, tport = "", 0
        allow = l.get("allow", [])
        if not isinstance(allow, list):
            p.append(f"{where}.allow must be a list")
            allow = []
        norm = []
        for a in allow:
            try:
                net = ipaddress.ip_network(a, strict=False)
                if net.version != 4:
                    raise ValueError
                norm.append(str(net))
            except ValueError:
                p.append(f"{where}.allow entry {a!r} is not an IPv4 CIDR")
        if len(norm) > 8:
            p.append(f"{where}.allow has more than 8 entries")
        hh = _check_host_header(p, where, l.get("host_header", ""), proto)
        tls, tv = _check_tls(p, where, l)
        listeners.append(Listener(lid, port, th, tport, proto, norm, hh, tls, tv, uni))
    if len(listeners) > MAX_LISTENERS:
        p.append(f"at most {MAX_LISTENERS} listeners")

    forwards = []
    fids, fports = set(), set()
    for i, f in enumerate(raw.get("forward", [])):
        where = f"forward[{i}]"
        fid = _int(p, where + ".id", f.get("id"), 1, 65535)
        if fid in fids:
            p.append(f"{where}.id {fid} is used twice")
        fids.add(fid)
        lh, lp = _hostport(p, where + ".listen", f.get("listen"), default_host="127.0.0.1")
        if (lh, lp) in fports:
            p.append(f"{where}.listen {lh}:{lp} is used twice")
        fports.add((lh, lp))
        proto = str(f.get("protocol", "http"))
        if proto not in ("http", "tcp"):
            p.append(f"{where}.protocol must be http or tcp")
        uni = _check_universal(p, where, f, proto)
        if uni:
            th, tp = "universal", 0
        else:
            th, tp = _hostport(p, where + ".target", f.get("target"))
            if th and (len(th) > 63 or any(c.isspace() for c in th)):
                p.append(f"{where}.target host must be at most 63 characters without spaces")
        ct = f.get("connect_timeout", host.connect_timeout)
        if isinstance(ct, bool) or not isinstance(ct, (int, float)) or not 0.1 <= ct <= 120:
            p.append(f"{where}.connect_timeout must be 0.1..120 seconds")
            ct = host.connect_timeout
        hh = _check_host_header(p, where, f.get("host_header", ""), proto)
        tls, tv = _check_tls(p, where, f)
        forwards.append(Forward(fid, lh, lp, th, tp, proto, float(ct), hh, tls, tv, uni))
    if len(forwards) > MAX_FORWARDS:
        p.append(f"at most {MAX_FORWARDS} forwards")
    if not listeners and not forwards:
        p.append("at least one [[listener]] or [[forward]] is required")

    if p:
        raise ConfigError(p)
    return Config(host, link, country.upper(), networks, hotspot, listeners, forwards)
