# Serial gateway, rev 5: Wi-Fi clients reach an HTTP API on the bridge PC's network

```
   WI-FI SIDE                                                      PC SIDE
   (network B)                                                     (network A)

  ┌────────────┐                                                 ┌────────────┐
  │  Clients   │                                                 │ API server │
  │  (SDKs,    │                                                 │ plain HTTP │
  │   curl)    │                                                 │ 10.0.0.5   │
  └─────┬──────┘                                                 └─────▲──────┘
        │                                                              │
        │ HTTP                                                    HTTP │
        │ over Wi-Fi                                                   │
        │                                                              │
  ┌─────▼──────┐        USB cable          ┌───────────────────────────┴──┐
  │            │   one COM port, no driver │                              │
  │   TETHER   │◄═════════════════════════►│   TETHER-HOST                │
  │   NODE     │   multiplexed frames      │   Python on Windows 11       │
  │            │                           │                              │
  │  ESP32-S3  │◄──── config (TOML) ───────│   tether.toml                │
  │  firmware  │      pushed at every      │   (Wi-Fi, hotspot,           │
  └────────────┘      start                │    port -> target)           │
                                           └──────────────────────────────┘
```

Flow:

```
  Client ─► Node:8000 ─► USB ─► Host ─► API
  Client ◄─ Node ◄───── USB ◄─ Host ◄─ API      (streaming passes straight through)
```

What each side sees:

```
  Wi-Fi network  ->  sees only the node          PC network  ->  sees only the PC
```

Fallback:

```
  Known Wi-Fi found ──► join it (DHCP)
  Not found, hotspot configured ──► node starts its own hotspot "tether"
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
* **tether-node: firmware (ESP-IDF, `tether-node/`)**: TinyUSB CDC-ACM only; Wi-Fi STA with credentials in NVS; N listeners configured by the PC
  at connect time; channel table with credit windows; mDNS; source allow-list (CIDR) enforced before accepting;
  host-silence watchdog (close everything, keep listening state so clients get immediate refusals, not hangs).
* **tether-host: Python (`tether-host/`, 3.10+, `pip install pyserial`, asyncio)**: serial link with auto-reconnect, framing/mux, listener config
  (`tether-host --listen 8000=http://10.0.0.5:8000`), Wi-Fi provisioning, status/log. The API itself needs no change.

## Properties for the OpenAI-style workload
* Long first-token latency is fine: no idle timeout on established connections; TCP keepalive on Wi-Fi sockets,
  2 s `PING` on the serial link. Connections end only on FIN/RST, watchdog or link loss (clients see a reset and retry).
* Bandwidth ~0.8-1 MB/s total (USB full speed), far above token streams; large prompts take seconds at worst.
* Capacity: 16 simultaneous connections including idle keep-alive ones. When full the ESP32 refuses new connections
  (clients see a reset); idle (between requests) keep-alive connections are closed after 60 s without data, never mid-stream.
* HTTP is not parsed, so the API cannot see the real client IP (no `X-Forwarded-For`); optional later via a minimal
  header injector on the PC. Authentication is the API's own (bearer key); the allow-list limits who on B can connect.

## Node network connectivity (DHCP station, hotspot fallback)

```
BOOT -> radio idle until CONFIG arrives from tether-host
          |
        STA connect (DHCP) --ok--> ONLINE (STA)
          | fail / no AP seen for 30 s (and [hotspot] configured)
        HOTSPOT (AP+STA): AP up, STA keeps retrying in the background
          | STA gets an IP and no hotspot client is connected
        ONLINE (STA), AP shut down
```

* **Normal mode**: Wi-Fi station, IP by DHCP from network B, mDNS name `tether-XXXX.local` (XXXX = last MAC bytes).
  Up to 4 networks from the config; scan, join the strongest one that is in range.
* **Fallback hotspot**: SSID `tether-XXXX`, **WPA2 with a per-device password** (open would expose the API). The node
  runs a DHCP server on the AP side (`10.42.77.1/24`, unusual on purpose; re-addressed if the STA network collides with it).
  The **listeners work on the hotspot too**: a client that joins it reaches the API exactly as on a normal network, so it
  doubles as a standalone mode when no infrastructure exists.
* **Provisioning**: only via the TOML config pushed by `tether-host` (see Configuration). Nothing is stored on the node, so
  there are no Wi-Fi secrets at rest in its flash. Until the first CONFIG arrives the radio stays off. A web page on the
  hotspot is not planned.
* **Radio limits (single radio)**: while the hotspot is up the AP follows the STA's channel, and background STA scans briefly
  disturb hotspot clients. So retries run every 60 s, and not at all while a hotspot client is mid-session (timer paused until idle).
* **Reconnect**: backoff 1, 2, 4 ... 60 s. Listeners bind to any address, so a changed IP or a switch between STA and
  hotspot needs no re-listen; established connections do reset. Power save is off (`WIFI_PS_NONE`) for streaming latency.
* **Policy knobs** (all from the TOML): fallback delay (30 s), `hotspot = auto|always|never`, hotspot password,
  country code (e.g. DE, for channels 1-13). No `[hotspot]` section means no hotspot at all (safe default).
* **Allow-list** applies on both interfaces (the hotspot is protected by its password; STA by CIDR list).
* **Status** (`WIFI_STATUS`): mode, SSID, IP, RSSI, hotspot client count, so `tether-host status` shows where to connect.
* Limits: 2.4 GHz only; no WPA2-Enterprise/captive portals in v1; hidden SSIDs need the name saved explicitly.

## Configuration (one TOML file, applied on every start)

```toml
[wifi]
country = "DE"
[[wifi.network]]
ssid = "NetworkB"
psk  = "..."
[[wifi.network]]            # optional further networks (max 4)
ssid = "Fallback"
psk  = "..."

[hotspot]                   # omit this section to disable the hotspot
mode     = "auto"           # auto | always | never
ssid     = "tether"         # default tether-XXXX
psk      = "..."            # required, 8+ characters
fallback = 30               # seconds without STA before the hotspot starts

[[listener]]
id = 1
port = 8000
target = "10.0.0.5:8000"
allow = ["192.168.1.0/24"]
```

* `tether-host` loads the file and sends it as one **CONFIG** message (full replacement, JSON payload) every time the
  link comes up: host start, USB replug, or the node resetting (it announces itself with HELLO, flag `unconfigured`).
  The node answers **CONFIG_RES** with a per-part status (e.g. wifi ok, listener 2 port in use).
* Apply is a diff: unchanged Wi-Fi settings do not drop the connection; changed listeners are closed and reopened.
* Editing the file takes effect on `tether-host` restart (or `tether-host reload`, later). Passwords live only in the
  file on the PC (restrict its permissions) and in the node's RAM.
* Consequence: with the PC off the node does nothing useful (no Wi-Fi, no listeners), which is fine since every request
  needs the PC anyway. `tether-host status` shows mode, SSID, IP, RSSI, hotspot clients.

## Wire protocol (implemented and tested in `tether-host/tether_host/protocol.py`)
`COBS(type | flags | chan u16 | payload<=1024 | crc32) 0x00`, resync at every 0x00. Used here: HELLO, PING/PONG,
CONFIG/CONFIG_RES (whole TOML as JSON), WIFI_STATUS, ACCEPT, DATA, CREDIT (per-channel flow control, 8 KiB window), CLOSE.
OPEN_TCP/OPEN_UDP/RESOLVE/DGRAM stay reserved for later outbound/UDP use.

## Build plan
1. Done: framing + tests.  2. Host: serial link, mux, credit flow control, connect-to-API.
3. Firmware: CDC-ACM, framing, Wi-Fi, listeners, channels, mDNS.
4. End-to-end: OpenAI SDK on B -> streaming chat completion -> API on A.
5. Allow-list, idle reaping, soak/hot-unplug tests.  6. Later: UDP, header injection.
