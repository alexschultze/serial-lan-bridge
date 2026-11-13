"""Command line: run | status | ports | check-config | bootloader."""
import argparse
import json
import logging
import os
import stat
import sys
import time

from . import __version__
from .config import ConfigError, find_config, load_config, state_dir

# asyncio, pyserial and the session code are imported only by the commands that need them, so the first message
# appears without waiting for those imports (slow on a first start on Windows, when Python compiles and scans them).
from .errors import SessionError

log = logging.getLogger("tether")


def _load(args):
    path = find_config(args.config)
    log.info("configuration: %s", path)
    cfg = load_config(path)
    if os.name == "posix" and os.stat(path).st_mode & (stat.S_IRGRP | stat.S_IROTH):
        logging.getLogger("tether").warning("%s is readable by other users and contains passwords (chmod 600)", path)
    return path, cfg


def _port(cfg, override=None):
    from .link import find_ports
    sel = override or cfg.host.port
    ports = find_ports(sel, cfg.host.serial)
    if not ports:
        raise SessionError("no tether-node found (is it plugged into the native USB port? see `tether-host ports`)")
    if len(ports) > 1:
        raise SessionError("several candidate ports: " + ", ".join(ports) + "; set host.port or host.serial")
    return ports[0]


def cmd_ports(args):
    from serial.tools import list_ports
    from .link import find_ports
    found = find_ports("auto")
    for p in list_ports.comports():
        mark = "*" if p.device in found else " "
        vid = f"{p.vid:04X}:{p.pid:04X}" if p.vid is not None else "----:----"
        print(f"{mark} {p.device:<14} {vid}  {p.product or p.description}  {p.serial_number or ''}")
    print("\n* = looks like a tether-node")
    return 0


def cmd_check(args):
    path, cfg = _load(args)
    print(f"{path}: OK ({len(cfg.networks)} network(s), {len(cfg.listeners)} listener(s), "
          f"hotspot {'on' if cfg.hotspot else 'off'}, node config {len(cfg.node_json())} bytes)")
    return 0


def cmd_run(args):
    path, cfg = _load(args)
    log.info("configuration OK: %d listener(s), %d forward(s), hotspot %s", len(cfg.listeners), len(cfg.forwards),
             "on" if cfg.hotspot else "off")
    import asyncio
    import signal
    from .link import SerialTransport
    from .session import Session
    sdir = state_dir()
    status_path = os.path.join(sdir, "status.json")

    async def main():
        async def opener():
            port = _port(cfg, args.port)
            log.info("opening %s", port)
            return SerialTransport(port)
        session = Session(cfg, opener, status_path)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
            if sig is not None:
                try:
                    loop.add_signal_handler(sig, session.stop)
                except NotImplementedError:   # Windows: Ctrl+C raises KeyboardInterrupt instead
                    pass
        await session.run_forever()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    return 0


def cmd_status(args):
    path = os.path.join(state_dir(), "status.json")
    try:
        with open(path) as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        print("tether-host is not running (no status file)")
        return 1
    age = time.time() - doc.get("updated", 0)
    node = doc.get("node") or {}
    stale = age > 15
    print(f"host:  pid {doc.get('pid')}, link {doc.get('link')}{' (STALE, last update %.0f s ago)' % age if stale else ''}, "
          f"{doc.get('host', {}).get('channels', 0)} channel(s)")
    if node:
        print(f"node:  {node.get('version')}  state {node.get('state')}  mode {node.get('mode')}")
        print(f"wifi:  {node.get('ssid') or '-'}  ip {node.get('ip') or '-'}  rssi {node.get('rssi')}  host {node.get('hostname')}")
        print(f"       hotspot clients {node.get('ap_clients')}  channels {node.get('channels')}  heap free {node.get('heap_free')}")
        print(f"       counters {node.get('counters')}")
        if node.get("drive") and node["drive"] != "off":
            print(f"drive: 1 MB USB drive, currently used by the {'PC' if node['drive'] == 'pc' else 'node'}")
        if node.get("note"):
            print(f"wifi problem: {node['note']}")
    return 2 if stale else 0


def cmd_bootloader(args):
    _, cfg = _load(args)
    import asyncio
    from . import protocol as P
    from .link import SerialTransport
    from .session import VERSION

    async def main():
        t = SerialTransport(_port(cfg, args.port))
        t.write(P.encode(P.hello(P.Role.HOST, P.NodeState.NEEDS_CONFIG, 1, VERSION)))
        t.write(P.encode(P.Frame(P.T.REBOOT, 0, bytes([1]))))
        await asyncio.sleep(0.5)
        await t.close()
    asyncio.run(main())
    print("the node restarts into download mode; flash it with idf.py / esptool")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tether-host", description=__doc__)
    ap.add_argument("--config", "-c", help="path of tether.toml")
    ap.add_argument("--log-level", help="debug|info|warning|error (overrides host.log_level)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, help_ in (("run", cmd_run, "run the bridge"), ("status", cmd_status, "show the running bridge's status"),
                            ("ports", cmd_ports, "list serial ports"), ("check-config", cmd_check, "validate the configuration"),
                            ("bootloader", cmd_bootloader, "restart the node into download mode")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--port", help="serial port, overrides host.port")
        # also accepted after the command (`tether-host run --config x.toml`); SUPPRESS keeps a value given before it
        sp.add_argument("--config", "-c", default=argparse.SUPPRESS, help="path of tether.toml")
        sp.add_argument("--log-level", default=argparse.SUPPRESS, help="debug|info|warning|error")
        sp.set_defaults(fn=fn)
    args = ap.parse_args(argv)
    level = args.log_level
    if not level and args.cmd in ("run", "bootloader"):
        try:
            level = load_config(find_config(args.config)).host.log_level
        except ConfigError:
            level = "info"
    logging.basicConfig(level=getattr(logging, (level or "info").upper()), stream=sys.stderr,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    if args.cmd in ("run", "bootloader"):
        log.info("tether-host %s starting (Ctrl+C stops it)" if args.cmd == "run" else "tether-host %s", __version__)
    try:
        return args.fn(args)
    except (ConfigError, SessionError, OSError) as e:
        if isinstance(e, ConfigError):
            for p in e.problems:
                print(f"config: {p}", file=sys.stderr)
        else:
            print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
