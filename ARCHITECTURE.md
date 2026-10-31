# Serial gateway, rev 5: Wi-Fi clients reach an HTTP API on the bridge PC's network

```
 Network B (Wi-Fi)                ESP32-S3                          BRIDGE PC (Windows 11)                Network A
┌───────────────────┐ HTTP  ┌───────────────────┐ USB CDC-ACM ┌──────────────────────────┐  HTTP   ┌──────────────┐
│ OpenAI-style      │──────►│ TCP listener(s)   │◄─ COM port ►│ Python: mux, flow ctl,   │────────►│ API server   │
│ clients           │◄──────│ :8000 (+ mDNS)    │ inbox usbser│ connects to API target   │◄────────│ (plain http) │
└───────────────────┘       └───────────────────┘             └──────────────────────────┘         └──────────────┘
 sees: ESP32 IP (DHCP)       Windows sees: one COM port only                              API sees: the PC as client
```

## Direction and NAT semantics
* **Destination NAT**: `ESP32_wifi_ip:8000 -> api_host_in_A:port`, carried over the serial link. A client on B talks to what looks like
  the API server itself.
* **Source NAT**: the API server sees every request coming from the PC. Neither network learns the other's addresses; the
  ESP32's B-side IP (DHCP, assigned by network B) is never used or known on network A.
* Windows sees exactly one COM port (CDC-ACM only; no network adapter, no driver install).
* Connection-level translation only: HTTP/TCP (and later UDP). No ping/broadcast across the bridge.

## Flow (one request)
1. Client opens TCP to `ESP32:8000`; lwIP accepts, firmware allocates a channel (limit 16) and sends `ACCEPT(chan, listener, peer)`.
2. Python looks up the listener's target and opens TCP to the API. On failure it replies with a short
   `HTTP/1.1 502` body, then `CLOSE`; on success both directions pipe through `DATA` frames with credit flow control.
3. Streaming (SSE) is not parsed or buffered: each socket read becomes one frame immediately (ESP32 `TCP_NODELAY`; the
   PC writes through and awaits `drain()` only for backpressure). Added latency ~2-5 ms per chunk.
4. FIN/RST on either side becomes `CLOSE` on the other.

## Finding the ESP32 from network B
Its address is whatever network B's DHCP assigns. Make it findable: **mDNS** name (`llm-bridge.local`; works on most
OSes, not all containers), plus a **DHCP reservation by MAC** on the router if available. Python `status` shows the current IP.

## Components
* **Firmware (ESP-IDF)**: TinyUSB CDC-ACM only; Wi-Fi STA with credentials in NVS; N listeners configured by the PC
  at connect time; channel table with credit windows; mDNS; source allow-list (CIDR) enforced before accepting;
  host-silence watchdog (close everything, keep listening state so clients get immediate refusals, not hangs).
* **Python (3.10+, `pip install pyserial`, asyncio)**: serial link with auto-reconnect, framing/mux, listener config
  (`--listen 8000=http://10.0.0.5:8000`), Wi-Fi provisioning, status/log. The API itself needs no change.

## Properties for the OpenAI-style workload
* Long first-token latency is fine: no idle timeout on established connections; TCP keepalive on Wi-Fi sockets,
  2 s `PING` on the serial link. Connections end only on FIN/RST, watchdog or link loss (clients see a reset and retry).
* Bandwidth ~0.8-1 MB/s total (USB full speed), far above token streams; large prompts take seconds at worst.
* Capacity: 16 simultaneous connections including idle keep-alive ones. When full the ESP32 refuses new connections
  (clients see a reset); idle (between requests) keep-alive connections are closed after 60 s without data, never mid-stream.
* HTTP is not parsed, so the API cannot see the real client IP (no `X-Forwarded-For`); optional later via a minimal
  header injector on the PC. Authentication is the API's own (bearer key); the allow-list limits who on B can connect.

## Wire protocol (implemented and tested in `host/serialbridge/protocol.py`)
`COBS(type | flags | chan u16 | payload<=1024 | crc32) 0x00`, resync at every 0x00. Used here: HELLO, PING/PONG,
WIFI_SET/WIFI_STATUS, LISTEN (set up a listener), ACCEPT, DATA, CREDIT (per-channel flow control, 8 KiB window), CLOSE.
OPEN_TCP/OPEN_UDP/RESOLVE/DGRAM stay reserved for later outbound/UDP use.

## Build plan
1. Done: framing + tests.  2. Host: serial link, mux, credit flow control, connect-to-API.
3. Firmware: CDC-ACM, framing, Wi-Fi, listeners, channels, mDNS.
4. End-to-end: OpenAI SDK on B -> streaming chat completion -> API on A.
5. Allow-list, idle reaping, soak/hot-unplug tests.  6. Later: UDP, header injection.
