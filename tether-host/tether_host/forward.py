"""Forwarded ports: a local port on the PC carried to an endpoint on the node's Wi-Fi network."""
import asyncio
import logging

from . import protocol as P
from .bridge import RESP_503, OutboundChannel

log = logging.getLogger("tether.forward")


class Forwarder:
    def __init__(self, cfg, session):
        self.cfg = cfg
        self.s = session
        self._servers = []

    async def start(self):
        for f in self.cfg.forwards:
            try:
                srv = await asyncio.start_server(lambda r, w, f=f: self._on_client(f, r, w), f.listen_host, f.listen_port)
            except OSError as e:
                await self.stop()
                raise OSError(f"forward {f.id}: cannot listen on {f.listen_host}:{f.listen_port}: {e}") from e
            self._servers.append(srv)
            log.info("forward %d: %s:%d -> %s:%d via the node", f.id, f.listen_host, f.listen_port, f.target_host, f.target_port)
            if f.exposed:
                log.warning("forward %d listens on %s, which other machines can reach (use 127.0.0.1 to keep it local)",
                            f.id, f.listen_host)

    async def stop(self):
        for srv in self._servers:
            srv.close()
        for srv in self._servers:
            await srv.wait_closed()
        self._servers.clear()

    async def _on_client(self, f, reader, writer):
        s = self.s
        cid = s.alloc_host_chan() if s.running.is_set() else None
        if cid is None:
            log.info("forward %d: refusing a client (%s)", f.id, "bridge not ready" if not s.running.is_set() else "no free channel")
            if f.http:
                writer.write(RESP_503)
                try:
                    await asyncio.wait_for(writer.drain(), 2)
                except (OSError, asyncio.TimeoutError):
                    pass
            writer.close()
            return
        peer = writer.get_extra_info("peername")
        peer = f"{peer[0]}:{peer[1]}" if peer else "local"
        s.channels[cid] = OutboundChannel(s, cid, f, peer, self.cfg.link.window, reader, writer)
