# Architecture and specification

Status: v1 implemented and tested without hardware (Python host, portable C node core, Linux simulator); the ESP32 glue
(USB, Wi-Fi) compiles for the ESP32-S3 but has not been run on a board (see README). This document is the
single source of truth; where code and text disagree, fix one of them. Items marked **(verify)** depend on
vendor behaviour that must be confirmed during implementation.

## 1. Purpose and requirements

Clients on a Wi-Fi network (**network B**) must reach an HTTP API (OpenAI-style completions, including streaming
SSE) that lives on the network of a Windows 11 PC (**network A**). The only link between the two is a USB cable
between the PC and an ESP32-S3. Hard requirements, all of which shaped the design:

| # | Requirement | Consequence |
|---|---|---|
| R1 | Windows 11 PC, **no additional drivers** | ESP32-S3 uses its native USB as **CDC-ACM** (inbox `usbser.sys`). Python uses `pyserial` (a library, not a driver). |
| R10 | A small **file storage** (1 MB, FAT) the PC can use | The same USB device also offers a USB drive (mass storage, inbox Windows driver), section 5.4. |
| R2 | The device must **not appear as a network adapter** in Windows | No USB Ethernet/RNDIS/NCM. No IP interface on the PC for the bridge, so no packet-level NAT or L2 bridge is possible. |
| R3 | Behaves like a **NAT** (neither network learns the other's addresses) | Connection-level NAT: destination NAT on the node (listener port to target) and source NAT (the API sees the PC; network B sees the node). |
| R4 | The ESP32 **joins a network by DHCP**, and can **advertise a hotspot** when none is found | Wi-Fi STA with AP fallback, section 7. |
| R5 | Primary traffic: **plain HTTP, including streaming** | No HTTP parsing, no buffering, low per-chunk latency; TCP only in v1. |
| R6 | Bandwidth below 1 MB/s is acceptable | USB full speed (12 Mbit/s) is sufficient. |
| R7 | **One TOML file** configures everything and is applied on **every (re)start** | Node holds no persistent configuration and no secrets in flash, section 6. |
| R8 | Flash from Linux or Windows | Standard ESP-IDF tooling, section 12. |
| R9 | **Bidirectional**: also a **local port on the PC** that is carried to an **endpoint on the node's network**, flexibly configured in the same file | Second channel type: the host opens connections, the node connects out (`[[forward]]`, OPEN/OPEN_RES), sections 4.6b, 6 and 8. |

## 2. System overview

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

Flow, inbound (Wi-Fi clients reach the API on the PC's network):

```
  Client ─► Node:8000 ─► USB ─► Host ─► API
  Client ◄─ Node ◄───── USB ◄─ Host ◄─ API      (streaming passes straight through)
```

Flow, outbound (a local program on the PC reaches an endpoint on the Wi-Fi network):

```
  Local app ─► 127.0.0.1:9000 ─► Host ─► USB ─► Node ─► 192.168.1.20:80 (device on network B)
  Local app ◄─ (loopback)    ◄─ Host ◄─ USB ◄─ Node ◄─ (the endpoint)
```

What each side sees:

```
  Wi-Fi network  ->  sees only the node          PC network  ->  sees only the PC
```

Fallback:

```
  Known Wi-Fi found ──► join it (DHCP)
  Not found, hotspot configured ──► node starts its own hotspot
```

**Roles.** The *node* owns everything on the Wi-Fi side: Wi-Fi, listeners, accepting client connections, the
allow-list. The *host* owns everything on the PC side: configuration, the target addresses, connecting to the API,
status display. The node never learns the targets (less information on the exposed device); the host never touches
Wi-Fi directly.

**NAT semantics.**
* Inbound destination NAT: `node_wifi_ip:listener_port` maps to `target_host:target_port`, held in the host's config.
* Outbound destination NAT (forward): `pc_loopback:listen_port` maps to `endpoint_host:endpoint_port` on network B. The endpoint
  sees the node's Wi-Fi address as the client; the local program only talks to loopback and learns nothing about network B.
* Source NAT: the API sees only connections from the PC (inbound); the endpoint sees only the node (outbound). Network B sees only the node's Wi-Fi address (DHCP, not
  known in advance; found via mDNS and `tether-host status`).
* Connection-level only: TCP in v1. No ICMP/ping, broadcast or multicast crosses the bridge, and UDP is reserved
  for a later version (message range `0x40-0x4F`).

## 3. Components and repository layout

```
serial-lan-bridge/
├── README.md
├── ARCHITECTURE.md              this file
├── tether.example.toml          annotated example configuration
├── tools/                       make_release.sh (release files), release_notes.md (template of the release text)
├── testvectors/                 frames.json (shared by Python and C tests), make_vectors.py
├── tether_host/                 Python 3.11+, depends on pyserial only
│   ├── pyproject.toml           entry point `tether-host`, `python -m tether_host`
│   ├── tether_host/
│   │   ├── protocol.py          framing and payload helpers (reference implementation)
│   │   ├── config.py            load/validate TOML, build the node CONFIG JSON
│   │   ├── link.py              Transport: pyserial (reader/writer threads) and in-memory loopback; port discovery
│   │   ├── session.py           handshake, PING/PONG, CONFIG push, STATUS, channel table, reconnect
│   │   ├── bridge.py            per-channel pipe (inbound Channel, outbound OutboundChannel), flow control, half-close, 502/503/504
│   │   ├── forward.py           local listening ports for [[forward]], channel id allocation
│   │   └── cli.py               run | status | ports | check-config | bootloader
│   └── tests/                   protocol, fake_node.py + test_e2e.py (Python node), test_sim_e2e.py (C engine)
└── tether-node/                 ESP-IDF project (C); platformio.ini, partitions.csv (with the 1 MB `storage` partition), sdkconfig.defaults
    ├── CMakeLists.txt, sdkconfig.defaults
    ├── components/tether_core/  portable C without ESP-IDF dependencies: tc_proto (COBS, CRC, deframer),
    │                            tc_json, tc_config (parse/validate), tc_engine (listeners, channels, flow control, control protocol)
    ├── main/                    ESP32 glue: usb_link (TinyUSB CDC-ACM), storage (USB drive), wifi_mgr (station/hotspot/mDNS), platform_esp, app_main
    ├── sim/                     Linux simulator: the same engine behind a pseudo-terminal; Makefile for tests and simulator
    └── tests/test_core.c        C unit tests
```

Boundaries: `tc_proto` (node) and `protocol.py` (host) are the only code that knows the byte format, and both
are tested against `testvectors/frames.json`.

## 4. Wire protocol

### 4.1 Framing

```
 wire := COBS( body ) 0x00
 body := type u8 | flags u8 | chan u16 LE | payload (0..1024 bytes) | crc32 u32 LE
```

* `crc32` is the standard IEEE CRC-32 (identical to Python `zlib.crc32`; on the node `esp_rom_crc32_le(0, buf, len)` **(verify against the test vectors)**) over `type..payload`.
* COBS removes all `0x00` from the body, so `0x00` is an unambiguous frame delimiter. After garbage, a mid-frame
  reset or a lost byte the receiver resynchronises at the next `0x00`.
* Limits: payload ≤ 1024; body ≤ 1032; encoded frame ≤ 1038 bytes plus delimiter. A receiver discards anything
  longer (`MAX_RAW` in `protocol.py`) and counts an error.
* Empty frames (two consecutive `0x00`) are ignored.
* Byte order is little-endian everywhere except IPv4 addresses (network order, as four bytes).
* USB CDC is a reliable ordered pipe, so no sequence numbers or retransmission exist. The CRC guards against bugs
  and electrical noise. See 4.8 for what happens when it fires.

### 4.2 Channels

* `chan = 0` is the control channel; the rest are data channels, one per connection. At most 16 are open at the same time on
  the node, inbound and outbound together.
* Inbound channels (a client connected to a node listener) have ids `1..16`, allocated by the **node** (lowest free id).
* Outbound channels (a local program connected to a forwarded port) have ids `0x8001..0x8010`, chosen by the **host**
  (lowest free). The two ranges cannot collide, so neither side needs to ask the other for an id.
* An id is reusable only after its channel has ended (4.6).

### 4.3 Message types

| Type | Name | Dir | Chan | Payload |
|---|---|---|---|---|
| 0x01 | HELLO | both | 0 | proto u8 · role u8 (0 host, 1 node) · state u8 (0 NEEDS_CONFIG, 1 RUNNING; host sends 0) · max_payload u16 · max_channels u8 · session u32 · version (UTF-8, ≤ 32 B) |
| 0x02 | PING | host→node | 0 | seq u32 |
| 0x03 | PONG | node→host | 0 | seq u32 (echo) |
| 0x10 | CONFIG | host→node | 0 | UTF-8 JSON (4.5), chunked: all chunks but the last carry `flags` bit 0 (`MORE`); total ≤ 8192 B |
| 0x11 | STATUS | node→host | 0 | UTF-8 JSON (7.6) |
| 0x12 | CONFIG_RES | node→host | 0 | UTF-8 JSON (4.5) |
| 0x13 | LOG | node→host | 0 | level u8 (0 error, 1 warn, 2 info, 3 debug) · UTF-8 text |
| 0x14 | REBOOT | host→node | 0 | mode u8: 0 normal restart, 1 restart into ROM download mode **(verify)** |
| 0x20 | ACCEPT | node→host | n | listener u16 · peer ip4 (4 B) · peer port u16 |
| 0x21 | OPEN | host→node | 0x8000+n | forward id u16 (the endpoint comes from the node's configuration, not from the message). Only for a forward configured `"any": true` (universal) it is followed by port u16 and the host name (1..63 printable ASCII bytes): the endpoint of this connection. Any other combination is answered with BAD_REQUEST |
| 0x22 | OPEN_RES | node→host | 0x8000+n | status u8: 0 OK, 1 REFUSED, 2 UNREACHABLE, 3 DNS, 4 NO_CAPACITY, 5 UNKNOWN_FORWARD, 6 BAD_REQUEST, 7 NOT_READY |
| 0x30 | DATA | both | n | stream bytes, 1..1024 |
| 0x31 | CREDIT | both | n | additional bytes the peer may send, u32 |
| 0x32 | CLOSE | both | n | reason u8: 0 EOF, 1 RESET, 2 PROTO, 3 NO_CHANNEL |

Unknown types on chan 0 are ignored (forward compatibility). A frame for a channel that is not open is dropped
and, unless it is itself a CLOSE, answered with `CLOSE(NO_CHANNEL)`. A receiver of `CLOSE(NO_CHANNEL)` treats it like RESET,
except for a channel on which both EOFs were already exchanged (a CREDIT that crossed the end of the channel): that one is
ignored, so queued data is still delivered.

### 4.4 Session start, restart and keepalive

```
 host                                             node
  │  (opens COM port, DTR asserted)                │
  │── HELLO(role=host, session=H) ────────────────►│  resets all channels; state := NEEDS_CONFIG
  │◄─ HELLO(role=node, state=0, session=N) ────────│
  │── CONFIG (1..8 chunks) ───────────────────────►│  validate, apply as a diff (section 6.4)
  │◄─ CONFIG_RES {ok, per-part status} ────────────│  state := RUNNING (if ok)
  │◄─ STATUS ... (on change and every 5 s) ────────│
  │── PING seq ───────────────────────────────────►│  every 2 s
  │◄─ PONG seq ────────────────────────────────────│
```

* Whoever's link comes up first sends HELLO. A node that boots (or loses the host, below) sends
  `HELLO(state=0)` once per second until a host HELLO or CONFIG arrives; a host that sees a node HELLO with
  `state = 0` **or a changed `session`** (node reset) sends CONFIG immediately. A host HELLO is a *session reset*:
  the node closes all channels and expects a CONFIG.
* `proto` must equal 1. On mismatch the host logs the versions and does not send CONFIG.
* CONFIG is re-sent if no CONFIG_RES arrives within 5 s (idempotent, up to 3 tries, then the host reconnects the port).
* Node watchdog: no valid frame from the host for **10 s** means **host lost**: close all channels, keep Wi-Fi and
  listeners as configured, send `HELLO(state=0)` every second, and answer new client connections as in 5.3. A
  valid frame from the host does not by itself resume RUNNING: the node waits for the host's HELLO and CONFIG.
* Host watchdog: no PONG for **6 s**, a serial error, or an unplugged device means **node lost**: close all channels
  (and their API connections), close the port, retry opening with backoff 0.5, 1, 2, 5 s (capped), and start over
  with HELLO.

### 4.5 CONFIG and CONFIG_RES (node-facing JSON)

The host builds this from the TOML (section 6). Host-only settings and all listener targets are *not* sent.

```json
{
  "v": 1,
  "wifi": {
    "country": "DE",
    "networks": [ {"ssid": "NetworkB", "psk": "secret", "hidden": false} ]
  },
  "hotspot": { "mode": "auto", "ssid": "", "psk": "secret123", "fallback": 30 },
  "listeners": [ {"id": 1, "port": 8000, "http": true, "allow": ["192.168.1.0/24"]} ],
  "forwards": [ {"id": 1, "host": "192.168.1.20", "port": 80} ],
  "link": { "window": 4096, "never_used_timeout": 30, "idle_timeout": 0 }
}
```

`listeners` and `forwards` may each be empty, but not both. A forward's `host` is an IPv4 address or a name resolved on the node's network.
`hotspot` is `null` when absent (no hotspot, ever). An empty `hotspot.ssid` means `tether-XXXX` derived by the node.
`allow` absent or empty means "same subnet as the receiving interface" (7.5). A missing `psk` means an open network.

Response (always sent, also on failure; the node keeps the previous configuration if validation fails):

```json
{ "ok": true,
  "wifi": "ok", "hotspot": "ok",
  "listeners": [ {"id": 1, "status": "ok"}, {"id": 2, "status": "port_in_use"} ],
  "errors": [] }
```

`ok` is false if any part is invalid (bad JSON, schema, limits, listener bind failure). A failed listener does not
stop the others. The host prints the result and exits with an error only if `ok` is false at start; it keeps
retrying on reconnect.

### 4.6 Data channels, half-close and ending

1. Client connects to a node listener; the node accepts, checks the allow-list and a free channel, then sends
   `ACCEPT(chan, listener, peer)`. Each direction starts with `link.window` bytes of credit; no CREDIT is needed
   to start.
2. Host looks up the listener's target and connects (connect timeout 5 s). Until connected, the host buffers node
   data (bounded by the window). On success it pipes both ways. On failure of an `http` listener it sends a
   `DATA` with `HTTP/1.1 502 Bad Gateway` (504 on timeout; `Content-Length: 0`, `Connection: close`), then `CLOSE(EOF)`;
   other listeners just send `CLOSE(RESET)`.
3. `CLOSE(EOF)`: the sender will send no more data (its socket reached EOF). The receiver, after flushing queued
   data, shuts down the write side of its own socket (`shutdown(WR)`). The channel is *finished* when both sides
   have sent `CLOSE(EOF)`; both then free it. If the second `CLOSE(EOF)` does not arrive within 60 s of the first,
   the side that sent the first sends `CLOSE(RESET)`.
4. `CLOSE(RESET)` / `CLOSE(PROTO)`: abort. The receiver closes its socket (TCP RST to the client), frees the
   channel at once and does not answer. A socket error on a side produces `CLOSE(RESET)`.
5. Ids are reused only after the channel is finished or aborted on the node (the node is the allocator). Since the
   link is ordered, a stale frame for a reused id cannot arrive after the new `ACCEPT`.

### 4.6b Outbound channels (forwards)

1. A local program connects to the forwarded port on the PC (`[[forward]]`, normally bound to `127.0.0.1`). If the link is not
   RUNNING or the host has no free channel, an `http` forward answers `HTTP/1.1 503`, a `tcp` forward just closes.
2. The host picks a channel id (`0x8001..0x8010`) and sends `OPEN(chan, forward id)`. It does not read from the local program
   yet.
3. The node looks the forward up in its configuration, allocates a channel and connects without blocking: an IPv4 literal is
   used directly; a name is resolved by a short-lived worker thread (so a slow DNS never stalls the other channels). It answers
   `OPEN_RES(chan, status)`. Errors: connection refused gives `REFUSED`; timeout, no route and network down give `UNREACHABLE`
   (the node gives up after 15 s); a failed lookup gives `DNS`; all channels busy gives `NO_CAPACITY`; an unknown forward id or
   one before the first CONFIG gives `UNKNOWN_FORWARD` or `NOT_READY`. A failed open leaves no channel behind.
4. The host waits for `OPEN_RES` at most `connect_timeout` (per forward, default 5 s). On timeout it sends `CLOSE(RESET)` (which
   also ends a connect still in progress on the node). On any failure an `http` forward answers the local program with 503
   (`NO_CAPACITY`, `NOT_READY`), 504 (host timeout) or 502 (all others), a `tcp` forward closes the connection.
5. On `OK` both directions start with `link.window` bytes of credit and the channel behaves exactly like an inbound one
   (data, credits, half-close, reset, 4.6 and 4.7).
6. A channel is finished only when both EOFs were exchanged **and** every queued byte has reached its stream, so a fast
   endpoint closing right after its last response cannot cut the data the local program has not yet received.

### 4.7 Flow control

* Credit is per channel and per direction, in bytes; a sender may transmit DATA only while its credit is positive and
  never more than the credit. Initial credit equals `link.window` (default 4096, range 1024 to 16384).
* A receiver returns credit with `CREDIT(n)` after it has *consumed* (written to its socket) at least `window / 2`
  bytes since the last grant. (Because the threshold is half the window, a sender can never be stuck with zero credit while
  unreturned credit exists, so no idle-time grant is needed.)
* A sender never reads from its socket while its credit is zero (the kernel buffer and TCP then back-pressure the
  peer). Sending more DATA than credit allows is a protocol error: `CLOSE(PROTO)`.
* Sizing: the window bounds node RAM (16 channels × 4 KiB × the host→node direction ≈ 64 KiB worst case) and, with
  a ~5 ms USB round trip, 4 KiB per channel sustains ≈ 0.8 MB/s.
* CONFIG, PING, STATUS and LOG are not flow-controlled (small, rate-limited: LOG ≤ 10 lines/s).

### 4.8 Link faults

* A deframer error (CRC mismatch, bad COBS, oversize) on a RUNNING session means data may have been lost inside a
  stream. The detecting side logs it, sends `CLOSE(RESET)` for every open channel if it can, and starts a new
  session: the host closes everything and sends HELLO; the node closes everything and sends `HELLO(state=0)`.
* A write to the USB endpoint that makes no progress for 5 s (host not reading) counts as host lost on the node.
* Counters (`crc_errors`, `rx_overflow`, `tx_stalls`) appear in STATUS and `tether-host status`.

## 5. tether-node (ESP32-S3 firmware)

### 5.1 Framework and build

ESP-IDF (C). Verified build: PlatformIO `espressif32@8.0.0` = ESP-IDF 6.1.0, managed components `esp_tinyusb` 2.4.0 and `mdns` 1.14.1 (esp_tinyusb 1.x does not build its mass-storage part against ESP-IDF 6)
(fetched from the Espressif component registry through `main/idf_component.yml`), `platformio.ini` in `tether-node/`. Plain `idf.py`
uses the same `CMakeLists.txt` and sources but has not been exercised. Other components: `esp_wifi`, `esp_netif`, `lwip`, `esp_timer`. Arduino, ESPHome and MicroPython were rejected (section 13). Key `sdkconfig.defaults`:

* Target module: **ESP32-S3-WROOM-1 N16R8** (16 MB quad flash, 8 MB octal PSRAM). PSRAM is added to the heap: allocations up to
  16 KB stay in internal RAM, larger ones and any overflow go to PSRAM, so even `link.window = 16384` on all 16 connections fits.
  For another module, change the flash size and the `CONFIG_SPIRAM*` lines (a wrong PSRAM mode makes the chip fail at boot).
* TinyUSB composite device: one CDC-ACM interface (the serial link) and one MSC interface (the USB drive, 5.4). **USB identity
  `1209:0001`**: the pid.codes vendor ID with its test product ID, set through Kconfig and mirrored in `tether-host`
  (`TETHER_VID`/`TETHER_PID` in `link.py`). Fine for own use; a project that is distributed registers its own PID at pid.codes (open
  source only) or asks Espressif for one. Windows caches the interface layout per VID:PID, so the PID must change whenever the set of
  USB functions changes. Strings: manufacturer `tether`, product `tether-node`, serial number = Wi-Fi MAC. CDC TX/RX buffers 2048.
* Console: UART0 (or disabled), **not** USB. ESP log output is additionally sent as `LOG` frames while a host is attached.
* `CONFIG_LWIP_MAX_SOCKETS` = 24 (16 channels + 4 listeners + margin; accepted by IDF 6.1.0); TCP send/receive buffers and window raised toward 16 KiB (start from the IDF iperf example).
* Wi-Fi power save off (`WIFI_PS_NONE`); task watchdog on; `PANIC_PRINT_REBOOT` (a crash yields a new `session`, which the host detects).
* Every setting in `sdkconfig.defaults` was checked to appear in the generated configuration (an unknown name is silently ignored).
* No persistent configuration: NVS is initialised only because the Wi-Fi driver calibration data needs it.

### 5.2 Tasks

| Task | Role |
|---|---|
| TinyUSB task / `rx_cb` | copies received CDC bytes into a stream buffer and signals an eventfd that the `net` task selects on |
| `net` | the engine (`tc_engine_poll`): one `select()` loop over the eventfd, listeners and channel sockets; owns the channel table (no locks); parses frames, applies the credit rules, runs the timers; it is also the only writer to CDC, so frames never interleave, and a write that makes no progress for 5 s counts as a stall (4.8) |
| DNS worker threads | one short-lived detached thread per outbound connect to a *name*; only `getaddrinfo`, then wake the `net` task |
| `wifi_mgr` | state machine of section 7 (500 ms tick); event handlers only update flags |

Per channel: socket fd, `credit_out` (what the node may still send to the host), `rx_out` (what the host may still send),
`consumed` (written to the socket since the last grant), a ring buffer of `window` bytes for host→node data awaiting `send()`, flags `eof_sent`, `eof_recv`,
timestamps. Client sockets use non-blocking mode, `TCP_NODELAY`, keepalive (idle 60 s, interval 10 s, 3 probes).

### 5.3 Behaviour rules

* **Streaming/latency:** every socket `recv()` of up to `min(1024, credit_out)` bytes becomes one DATA frame at
  once; no coalescing timers.
* **Accept filter:** drop (RST, no response) connections whose peer is not allowed (7.5).
* **Capacity / not running:** if all 16 channels are in use, or the node is in NEEDS_CONFIG / host-lost state, an
  accepted connection of an `http` listener gets a static `HTTP/1.1 503 Service Unavailable` (`Retry-After: 2`,
  `Content-Length: 0`, `Connection: close`) and is closed; non-http listeners are reset.
* **Never-used timeout** (`link.never_used_timeout`, default 30 s): a channel that has carried no data in either
  direction is reset (slowloris guard).
* **Idle timeout** (`link.idle_timeout`, default 0 = off): optional. Idle keep-alive connections are normally ended by
  the API server's own keep-alive timeout, which the host propagates as EOF. The node cannot tell an idle keep-alive
  connection from a streaming response that is waiting for the first token (minutes on a cold GPU worker), so it never
  reaps established channels by default.
* **Outbound connects** (section 4.6b): non-blocking `connect()`, completion through `select()`; DNS in a detached worker thread
  (at most 4 at a time, 4 KiB stack, result handed over through a small queue and a wake-up fd); the node's own limit is 15 s.
  A forward id that disappears or changes in a CONFIG resets its open channels. The endpoint is always taken from the node's
  configuration; the host cannot ask for an arbitrary address.
* **No other network services.** The node listens only on the configured ports; there is no web UI, OTA or debug server.

### 5.4 USB drive (file storage)

* Partition `storage` (data/fat, 1 MB at `0x310000`), wear levelling with 512-byte sectors (as in Espressif's USB drive example),
  FAT with long file names. Formatted automatically the first time.
* One owner at a time, switched by esp_tinyusb: while the PC has the device enumerated the volume is the PC's USB drive; otherwise it
  is mounted at `/data` for the firmware. The firmware does not use it yet; it is storage for the PC.
* STATUS reports the owner (`"drive": "pc" | "node" | "off"`), shown by `tether-host status`.
* It shares the USB full-speed link with the serial port: copying large files slows the bridge traffic down while it runs. As
  with any USB stick, unplugging during a write can corrupt the FAT; eject it in Windows first.

## 6. Configuration

### 6.1 File

One TOML file (`tether.toml`, path from `--config`, else `./tether.toml`, else `%APPDATA%\tether\tether.toml` on
Windows or `~/.config/tether/tether.toml`). Parsed with `tomllib` (Python 3.11+). Full example: `tether.example.toml`.

```toml
[host]                      # host-only, never sent to the node
port            = "auto"    # "auto" (match USB product "tether-node") or e.g. "COM7" / "/dev/ttyACM0"
serial          = ""        # optional USB serial number, to pick one node when several are plugged in
log_level       = "info"    # debug | info | warning | error
connect_timeout = 5         # seconds, connecting to a listener target

[link]                      # sent to the node
window             = 4096   # flow-control window per channel and direction (1024..16384)
never_used_timeout = 30     # seconds; 0 disables
idle_timeout       = 0      # seconds; 0 disables (default)

[wifi]
country = "DE"              # regulatory domain, two letters
[[wifi.network]]            # 1..4 entries; the strongest visible one is joined
ssid   = "NetworkB"
psk    = "..."              # omit for an open network
hidden = false

[hotspot]                   # omit the whole section for no hotspot
mode     = "auto"           # auto: only when the station cannot connect; always: also while connected
ssid     = ""               # default "tether-XXXX" (last two MAC bytes)
psk      = "..."            # required, 8..63 characters (WPA2)
fallback = 30               # seconds without a station connection before the hotspot starts

[[listener]]                # 1..4 entries
id       = 1                # 1..65535, unique
port     = 8000             # port opened on the node, 1..65535, unique
target   = "10.0.0.5:8000"  # host:port the PC connects to (name resolved by Windows)
protocol = "http"           # "http" (node/host may answer 503/502/504) or "tcp"
allow    = ["192.168.1.0/24"]  # who may connect; omitted = same subnet as the node's interface

[[forward]]                 # 0 to 8 forwards: a local port on the PC carried to an endpoint on the node's network
id              = 1
listen          = "127.0.0.1:9000"   # on the PC; a bare port number means 127.0.0.1:<port>
target          = "192.168.1.20:80"  # on the Wi-Fi network: IPv4 address or a name the node resolves there
protocol        = "http"             # "http" (503/502/504 replies to the local program) or "tcp"
connect_timeout = 5                  # seconds the node may take to connect; default host.connect_timeout
host_header     = "auto"             # optional, http only, PC side (never sent to the node): rewrite the Host header, see below
tls             = "connect"          # optional, PC side: HTTPS on the final leg to the target, see below
tls_verify      = true               # check the target's certificate (default true)
```

`tls = "connect"` (on a `[[forward]]` or `[[listener]]`): the client side stays plain HTTP and tether-host speaks HTTPS to the target.
For a listener the PC opens a TLS connection to the target; for a forward the bytes travel through the node, so tether-host runs the
TLS client itself over in-memory buffers (the node stays plain TCP and never sees a key). The server name for SNI and the
certificate check is the target's host name, the check uses the system certificate store, and only HTTP/1.1 is offered (the Host
rewriter does not parse HTTP/2). A TLS or certificate failure is logged and the local client gets `502`. `tls_verify = false` accepts
any certificate (logged as a warning): use it only for a target you cannot verify, such as a device with a self-signed certificate.
Together with `host_header = "auto"` this lets a plain `http://127.0.0.1:<port>` reach an HTTPS site by name.

**Universal forwarder** (`universal = true` on a `[[forward]]` or a `[[listener]]`, http only, no `target`, `host_header` or `tls`): the
destination of each connection is part of the first request's path, `/<http|https>/<host>[:port]/<rest>`. tether-host
reads that first request head, strips the prefix from every request on the connection (a later request with another prefix
resets the connection), sets `Host` to the destination (without the port when it is the scheme's default) and uses TLS for
`https` (certificate checking on; `tls_verify = false` switches it off). A path that names no destination gets `400`. For a
listener the PC connects to the destination itself. For a forward the destination travels in `OPEN` (4.6b) and the node, whose
config marks that forward `"any": true`, connects to it: **any host and port the PC names**, including devices on the Wi-Fi
network, so keep such a forward on `127.0.0.1` and use it only on a PC you trust. A forward without `universal` still
cannot make the node connect anywhere but its configured endpoint. Responses are not rewritten (redirects can leave the prefix).

`host_header` is also accepted on a `[[listener]]` (http only), where it rewrites the requests of the Wi-Fi clients on their way to the
target; everything below applies to both.

`host_header`: a forward carries bytes, so the server sees the client's own `Host` (for example `127.0.0.1:9000`), which servers
that host several sites or guard against DNS rebinding refuse. With `host_header` set, tether-host replaces the `Host` line of every
HTTP/1.x request on that forward: `"auto"` uses the target's name (plus `:port` unless the port is 80 or 443), any other value is used
as given. Requests are followed one by one (keep-alive); bodies are skipped by `Content-Length` or chunked framing and never
touched. The host stops rewriting and passes the rest of the connection through when it cannot follow it: not HTTP, a head over
64 KB, an `Upgrade` (WebSocket) or `CONNECT`, a malformed length. Responses are never changed, so a redirect still names the
server's own address, and `Origin`/`Referer` headers are not rewritten. HTTPS cannot be rewritten (the bytes are encrypted).

### 6.2 Validation (host side, before anything is sent; `tether-host check-config` runs only this)

Required: at least one `[[wifi.network]]`, and at least one `[[listener]]` or `[[forward]]`. SSID 1..32 bytes, PSK 8..63 characters (or absent),
hotspot PSK 8..63; at most 4 networks and 4 listeners; unique `id` and `port`; `target` is `host:port`; `allow`
entries are valid IPv4 CIDRs; `window` in range. Forwards: at most 8, unique `id` and `listen`, `listen` is `host:port` or a port,
`target` is `host:port` (host at most 63 characters, no spaces), `connect_timeout` 0.1 to 120 s. A `listen` address other than loopback is
allowed (the local port is then reachable from other machines) and logged as a warning. The node validates again and reports problems in CONFIG_RES.

### 6.3 Security of the file

Contains the Wi-Fi and hotspot passwords in clear text. Keep it readable only by the account that runs
`tether-host` (NTFS permissions on Windows, `chmod 600` on Linux). `tether-host` warns when the file is world-readable
on POSIX. The node never writes the configuration to flash.

### 6.4 Apply semantics on the node

CONFIG is a full replacement, applied as a **diff** against the running configuration:
* unchanged Wi-Fi settings → the station connection is not touched;
* changed Wi-Fi networks → reconnect;
* hotspot added/removed/changed → AP restarted (its clients drop);
* listener unchanged (same `id`, `port`, `allow`, `http`) → open channels survive; changed or removed → its listening
  socket and channels are closed; new → opened;
* forward unchanged (same `id`, `host`, `port`) → its channels survive; changed or removed → they are reset.
After a host-lost period or session reset, all channels are closed anyway (4.4), but Wi-Fi and listeners stay up.

### 6.5 Lifecycle

* Edit the file, then restart `tether-host` (a `reload` command is a later nicety).
* Node reset or USB replug: automatic, the host re-sends the same config (4.4).
* With the PC off the node has no configuration and does nothing (no Wi-Fi, no listeners): fine, since every request
  needs the PC.

## 7. Node network connectivity

### 7.1 State machine

```
BOOT ── radio off until the first valid CONFIG
          │
          ▼
     STA_CONNECTING ── got IP ─────────────► ONLINE (STA)
          │                                    │  (hotspot mode "always": AP also up)
          │ no network joined for `fallback`   │ disconnect
          │ seconds and [hotspot] configured   ▼
          ▼                                STA_CONNECTING
     HOTSPOT (AP + STA scan every 60 s)
          │ STA obtains an IP and the AP has no clients
          ▼
     ONLINE (STA), AP stopped
```

### 7.2 Station

* Up to 4 networks from the config. Scan; join the strongest visible known one (hidden entries are probed directly).
  Authentication: WPA2-PSK minimum (WPA3-SAE transition accepted); open network if `psk` is absent.
* IP by DHCP. No static IP option in v1.
* Reconnect backoff 1, 2, 4, 8, … capped at 60 s. Country code from the config is applied before any scan.
* Only 2.4 GHz (ESP32-S3). WPA2-Enterprise and captive portals are out of scope.

### 7.3 Hotspot

* SSID `hotspot.ssid` or `tether-XXXX`; **WPA2-PSK with the configured password, never open**; channel follows the
  station's channel when both are active (single radio), else channel 1/6/11 chosen by a quick scan.
* Address `10.42.77.1/24` with a DHCP server (leases `.2-.20`). If the station's subnet equals this subnet the AP uses
  `10.42.78.0/24` instead (and so on).
* Listeners are bound to all interfaces: clients that join the hotspot reach the API exactly as on a normal network.
* While the hotspot is up, STA scans are retried every 60 s and are postponed while the AP has an active client
  (data transferred in the last 30 s), because a scan briefly interrupts the AP.

### 7.4 Discovery on network B

* The node gets its address from network B's DHCP; clients find it via **mDNS**: hostname `tether-XXXX.local` (XXXX = last
  two MAC bytes, lower-case hex) and service `_tether._tcp` with TXT `ports=8000,9000`. Windows 10+/macOS/Linux
  (avahi) resolve `.local` names; some containers and Android versions do not. A DHCP reservation by MAC on the router
  is the robust alternative. `tether-host status` always shows the current IP.

### 7.5 Allow-list

`allow` is checked on the accepted peer address before a channel is allocated. Semantics: entries are IPv4 CIDRs; if
absent or empty, only peers inside the subnet of the interface that received the connection are allowed (so routed or
off-link sources are refused). `0.0.0.0/0` explicitly allows everyone. IPv6 is not supported.

### 7.6 STATUS JSON (every 5 s and on change)

```json
{ "state": "running", "mode": "sta", "ssid": "NetworkB", "ip": "192.168.1.57", "rssi": -58,
  "hostname": "tether-a1b2.local", "ap_clients": 0, "channels": 2, "uptime": 3612,
  "counters": { "crc_errors": 0, "rx_overflow": 0, "tx_stalls": 0, "accepted": 41, "refused": 3 },
  "heap_free": 148320, "version": "tether-node 0.1" }
```

`mode` is `off | sta | ap | ap+sta`; `state` is `needs_config | running | host_lost`.

## 8. tether-host (Python on the bridge PC)

### 8.1 Requirements

Python 3.11+, `pyserial >= 3.5`. Nothing else (`asyncio`, `tomllib`, `logging`, `argparse` are standard library).
Runs as a normal user (no admin rights, no drivers). Works on Windows 11 and Linux/macOS.

### 8.2 Structure

* **Transport** abstraction with a serial implementation and an in-memory loopback implementation. Real code and
  tests use the same session logic.
* **Serial transport:** discovery through `serial.tools.list_ports` (USB ID `1209:0001`, or a product string containing `tether` where the OS reports
  one; Windows' port list usually lacks it; an ESP32 in ROM download mode, `303A:1001`, is reported once with a hint; or the
  configured port; optionally filtered by USB serial number; several matches and no selector is an error that lists them). A reader thread
  (`read(max(1, in_waiting))`, short timeout) pushes bytes to the event loop; a writer thread drains an outbound queue
  (so asyncio never blocks on the COM port). Receive/transmit buffers are enlarged (`set_buffer_size`) on Windows. DTR/RTS stay at their defaults.
* **Session** (asyncio): handshake and watchdogs of 4.4, CONFIG push and result handling, STATUS cache, PING loop, channel table.
* **Bridge:** per ACCEPTed channel, `asyncio.open_connection(target)` with the connect timeout; two pump tasks move data
  between the stream and DATA frames according to 4.6/4.7; writes use `drain()` for backpressure; outbound frames from all
  channels pass through one write queue; each pump emits one frame per read, so channels interleave at frame granularity.
* **CLI:** `tether-host run [--config FILE]`, `status` (reads `status.json`, written by the running instance on every STATUS, in `%LOCALAPPDATA%\\tether` or `~/.local/state/tether`; reports stale data when older than 15 s; the COM port is exclusive, so it is never opened a second time), `ports` (lists candidate COM ports), `check-config`, `bootloader` (REBOOT mode 1, for re-flashing), `--log-level`.
* **Logging:** to stderr; node `LOG` frames appear with a `node:` prefix; one INFO line per connection (listener, peer, bytes, duration).

### 8.3 Running on Windows

Run from a terminal, or as a Scheduled Task at logon (no admin needed; "run whether user is logged on or not" needs the
password). `pythonw -m tether_host run` hides the window. A Windows Service is a possible later addition.

### 8.4 Throughput notes

Expected 0.8 to 1.0 MB/s total (USB full speed shares both directions; 12 Mbit/s bulk ceiling ≈ 1.2 MB/s), plus
2 to 5 ms latency per chunk. Fine for token streams; a 1 MB request body takes about 1 to 1.5 s.

## 9. Failure behaviour

| Event | Node | Host | Clients/API see |
|---|---|---|---|
| USB unplugged / node reset | (reboots, new `session`) | no PONG within 6 s or serial error: close all channels, reconnect with backoff, HELLO + CONFIG | connection reset; new requests work after reconnect (seconds) |
| Host process stops or crashes | after 10 s: host-lost, channels closed, answers 503 | – | reset, then 503 |
| Wi-Fi lost | reconnect with backoff (hotspot after `fallback`), listeners stay bound | STATUS shows it | resets for open connections |
| API target down | – | 502/504 for `http` listeners | HTTP 502/504 |
| Forward endpoint refuses, unreachable or unresolvable | `OPEN_RES` with the reason | 502 (504 on timeout) for `http` forwards, close for `tcp` | HTTP 502/504 or closed connection |
| Forwarded port used while the link is down | – | 503 (`http`) or close (`tcp`); the local port stays bound across reconnects | HTTP 503 |
| All 16 channels used | static 503 (inbound), `OPEN_RES NO_CAPACITY` (outbound) | 503 to the local program | HTTP 503 |
| CRC/framing error | resets session (4.8) | resets session | in-flight connections reset |
| Config invalid | keeps previous config, reports errors | prints the errors | – |
| Two nodes plugged in, no selector | – | refuses to start, lists ports | – |

In-flight streaming responses cannot be resumed after a fault; clients retry.

## 10. Security

* Threat model: any device on network B (or the hotspot) can try to reach the listeners; the USB link is physically trusted.
* Outbound: the endpoint of a forward comes from the node's configuration, never from a message, so a compromised PC cannot make the
  node connect anywhere else; the local port binds to `127.0.0.1` by default (other addresses are logged as a warning). Any local
  program or user on the PC can use a forwarded loopback port, so treat it like any local service.
* Mitigations: per-listener allow-list (default: same subnet), WPA2 on the hotspot (no open AP, ever), no node-side
  management services, no secrets persisted on the node, passwords only in the config file on the PC (permissions, 6.3),
  targets hidden from the node, bounded resources (16 channels, 4 listeners, never-used timeout).
* Not provided: **TLS between clients and the node.** The HTTP, including any API key in `Authorization` headers,
  crosses network B in clear text. Use a WPA2/WPA3 network you trust, and treat the API key as exposed to it. The API's
  own authentication still applies, since the bridge does not parse or alter requests. A TLS mode is a later extension.
* The API cannot see the real client address (everything arrives from the PC). No `X-Forwarded-For` is added in v1.

## 11. Testing strategy

| Level | What | Where |
|---|---|---|
| Unit (Python) | framing, payload helpers, chunked CONFIG | `tether_host/tests/test_protocol.py` |
| Shared vectors | Python and C decode and re-encode identical bytes | `testvectors/frames.json`; `make_vectors.py` only with a protocol version bump |
| Unit (C) | CRC, COBS, deframer faults, JSON, config validation, vectors; built with ASan/UBSan | `make -C tether-node/sim test` |
| Host against a Python node | streaming without buffering, 1.5 MB round trip, half-close, 502, 503 at 17 connections, node reset, framing fault, silent node | `tether_host/tests/test_e2e.py` with `fake_node.py` |
| Host against the real C engine | same over a pseudo-terminal and the real `SerialTransport`: streaming, transfers with 4 KiB and 1 KiB windows, half-close, 502/503, allow-list, never-used timeout, node killed and restarted, rejected Wi-Fi config; forwards by IP and by name (resolver thread), 1.5 MB both ways, refused and unresolvable endpoints, 8 outbound plus 8 inbound sharing the 16 channels | `tether_host/tests/test_sim_e2e.py` (needs `make` and a C compiler; Linux/macOS) |
| Hardware | checklist below | a board plus the bridge PC |

Hardware acceptance checklist (board: ESP32-S3-WROOM-1 N16R8, PC: Windows 11 without added drivers):

*Boot and USB*
1. The board boots with PSRAM enabled (UART console on the `COM` port shows no PSRAM error and no reboot loop).
2. The native `USB` port enumerates as `USB\VID_1209&PID_0001` with two functions, a "USB Serial Device" (COM port) and a USB drive,
   with no driver prompt. `tether-host ports` marks the COM port.

*Bridge*
3. `tether-host run` configures the node; the log shows the node's Wi-Fi address.
4. The OpenAI SDK streams a chat completion from a Wi-Fi client to the API, token by token.
5. A forward: a program on the PC reaches an endpoint on the Wi-Fi network through `127.0.0.1:<listen>`, by IP and by name.
6. 16 parallel requests (inbound and forwards mixed); the 17th gets 503.
7. 100 MB transfer: sustained ≥ 0.8 MB/s.
8. Unplug/replug during a stream: recovery without restarting the host.
9. Host killed: the node answers 503 within 10 s.
10. Wi-Fi AP powered off, then on: the node reconnects; a wrong password shows "authentication failed (wrong password?)" in the host log.
11. Unknown SSID with `[hotspot]` set: the hotspot appears after `fallback`; a client reaches the API through it.

*USB drive*
12. Explorer shows the drive with a FAT file system of a little under 1 MB, already formatted on first boot.
13. Files of different sizes, long file names included, can be written, read back unchanged (compare checksums) and deleted;
    filling the drive gives Windows' normal "not enough space" message.
14. Files survive ejecting and replugging, a board reset and a power cycle.
15. `tether-host status` shows the drive as used by the PC; after unplugging from the PC (board powered from elsewhere) the
    console reports that the node owns it, and after replugging that the PC owns it again.
16. Copying a large file to the drive during a streaming request: the stream continues (slower), no link reset or framing error.
17. Unplugging without ejecting during a write: the drive still mounts afterwards (Windows may offer to scan and repair it).

*Endurance*
18. 24 h soak with periodic requests: no heap growth in STATUS.

CI (`.github/workflows/ci.yml`): Python and C tests on Linux; a second job builds the firmware with PlatformIO and uploads the
image as an artifact.

## 12. Build, flash and operate

* **Build node:** `cd tether-node && pio run` (PlatformIO, verified). Plain ESP-IDF: `idf.py set-target esp32s3 && idf.py build` (same sources, not exercised).
* **First flash** (Linux or Windows): put the board in ROM download mode (hold BOOT, plug in USB or tap RESET). The chip's
  ROM enumerates as a USB serial device that Windows binds with its inbox driver, `pio run -t upload --upload-port <port>` (or `idf.py -p <port> flash`, or `esptool`). After the app boots, it re-enumerates as `tether-node` on a (possibly different) COM port.
* **Re-flash:** the application's USB port does not support esptool's automatic reset, so run `tether-host bootloader`
  (sends REBOOT mode 1) or repeat the manual BOOT procedure.
* **Which port on the board:** the one wired to the chip's native USB (GPIO19/20), usually labelled `USB`. Ports behind a
  CP210x/CH340/CH343 UART bridge (labelled `COM` or `UART`) are not usable for the bridge (driver may be required, ≈ 0.3 MB/s),
  but on boards with **two USB-C ports** that second port is useful: it carries the UART0 console (firmware logs) and supports
  esptool's automatic reset for flashing. After flashing, Device Manager shows the native port as `USB\VID_1209&PID_0001`.
* **Debugging:** logs over UART0 and as `LOG` frames. The chip's built-in JTAG shares the USB pins with TinyUSB, so source-level
  debugging needs an external JTAG adapter or a development build that uses the built-in USB-Serial/JTAG instead of TinyUSB.
* **Updates:** USB flashing only (no OTA, by design: no network services on the node).
* **Distribution:** GitHub Releases. Pushing a tag `vX.Y.Z` (or `vX.Y.Z-a1/-b1/-rc1`, published as a pre-release) runs
  `.github/workflows/release.yml`: tests, then `tools/make_release.sh X.Y.Z`, then `gh release create` with three files:
  * `tether-node-X.Y.Z-esp32s3-n16r8.bin`: one merged image (bootloader at `0x0`, partition table at `0x8000`, app at `0x10000`, offsets
    read from the build), flashed at `0x0`. It ends with the application, so the `storage` partition and the files on the USB drive
    survive an update; `esptool erase-flash` would delete them. The NVS area inside the image range is reset, which only costs the Wi-Fi
    driver a recalibration (the node stores nothing else).
  * `tether-host-X.Y.Z.zip`: the `tether_host` package, `pyproject.toml`, a short README and `tether.example.toml`; `pip install .` in it.
  * `SHA256SUMS.txt`.
* **Versions:** the tag is the single source. The firmware gets it through `TETHER_VERSION` at build time (`"tether-node X.Y.Z"` in HELLO,
  `"dev"` for local builds); the zip gets it stamped into `tether_host.__version__`, which `pyproject.toml` reads. tether-host logs a
  warning when the node's firmware comes from another release. Versions are limited to `X.Y.Z` with an optional `-aN/-bN/-rcN`, which
  is valid for both the firmware string and Python packaging (PEP 440).
* **Template:** `tether.example.toml` marks every value to fill in as `<...>`; `check-config` refuses the file until none is left, and
  optional parts (hotspot, forwards) are commented out, so an unedited copy can never start a hotspot with a published password.
  Tests check the template stays that way.

## 13. Decision log

| Decision | Chosen | Rejected and why |
|---|---|---|
| Link to the PC | USB CDC-ACM serial | UART bridge chip (driver, 0.3 MB/s); USB Ethernet/NCM/RNDIS and USB Wi-Fi bridge designs (violate R2); WinUSB vendor class (needs libusb on the PC) |
| Translation level | connection-level NAT in user space | packet NAT / L2 bridge (needs a network adapter or driver on the PC, violates R1/R2); transparent proxy for arbitrary apps (impossible without an IP interface) |
| Direction | node listens on Wi-Fi, host connects to the API | outbound proxy (HTTP CONNECT/SOCKS) on the PC: the API is on the PC's side and the clients are on Wi-Fi, so it was the wrong direction |
| Config location | single TOML on the PC, pushed on every start, not stored on the node | NVS-stored networks/web provisioning (secrets at rest, more code); build-time config |
| Framing | COBS + CRC-32 + `0x00` delimiter | length-prefixed frames (no resync); SLIP (variable overhead, no CRC); HDLC |
| Flow control | per-channel credits | none (one slow client would block all); TCP-style windows over the link (overkill) |
| Half-close | `CLOSE(EOF)` both ways, `RESET` for abort | full close only (breaks clients that shut down their write side) |
| Idle reaping | off by default; never-used timeout only | blanket 60 s reaping (kills streams waiting for the first token) |
| Node config scope | inbound targets stay on the PC; outbound endpoints are in the node's configuration and OPEN carries only an id | targets on the node for inbound (exposes internal addresses on a Wi-Fi device); free-form addresses in OPEN (would let a compromised PC scan network B through the node) |
| Outbound channel ids | host-chosen range `0x8001..0x8010`, separate from the node's `1..16` | one id space with a request/response id exchange (extra round trip), or a shared range (races) |
| DNS for outbound names | worker thread per lookup | blocking `getaddrinfo` in the net task (a dead DNS server would stall every connection for seconds and trip the host watchdog), the lwIP raw DNS API (not portable to the Linux simulator) |
| Framework | ESP-IDF (C) | Arduino-ESP32 (less control of USB/lwIP), ESPHome (build-time config, own services, USB device support unclear; custom component would carry the whole product anyway), MicroPython (speed, memory, REPL conflicts), Rust (tooling cost) |
| Host language | Python 3.11+, pyserial only | other languages: Python is the PC environment given by the requirement |
| Hotspot | optional, WPA2 only, off unless configured | always-on or open AP (exposes the API) |
| USB identity | `1209:0001` (pid.codes test PID), one constant on each side | Espressif VID with TinyUSB's default PID (shared by every ESP32 TinyUSB gadget, and it changes when USB functions are added); an arbitrary PID under Espressif's VID (may collide with an allocated product) |
| File storage | USB drive (mass storage) on a 1 MB FAT partition, owner switched automatically | a network share (needs a network adapter, excluded by R2); files only through the serial protocol (needs a client tool) |
| Transport security | none in v1 (documented) | TLS on the ESP32 (RAM, certificates, complexity); possible later |

## 14. Open points to verify during implementation

1. Windows 11 enumerates the composite device (`1209:0001`) as a "USB Serial Device" plus a USB drive with no driver prompt; the
   drive's 512-byte sectors and the FAT format work in Explorer.
2. RAM budget with 16 channels plus Wi-Fi AP+STA under load (the idle build uses 15% of RAM; `heap_free` in STATUS shows the rest).
3. Real USB CDC throughput with `pyserial` on Windows 11 (target ≥ 0.8 MB/s) and the best CDC buffer sizes.
4. Software reset into ROM download mode on the S3 (REBOOT mode 1) from running TinyUSB firmware.
5. AP+STA behaviour (channel following, scan interruptions) with real access points.
6. The node uses its own CRC-32 (`tc_crc32`, verified against the vectors), so no ESP-ROM function is needed.
7. The ESP32 glue (`usb_link.c`, `wifi_mgr.c`, `platform_esp.c`) compiles and links against ESP-IDF 6.1.0 and esp_tinyusb 2.4.0, but has
   never run: USB enumeration, the drive, throughput, PSRAM, Wi-Fi fallback and mDNS need a board.

## 15. Roadmap

1. Done: framing, payload helpers, shared test vectors, unit tests.
2. Done: tether-host (config, transports, session, bridge, CLI) with end-to-end tests against a Python node and the C engine.
3. Done: portable node core in C (protocol, JSON, config, engine) with unit tests and the Linux simulator.
4. Done to the extent possible without a board: ESP32 glue (USB CDC, Wi-Fi station/hotspot/mDNS, platform hooks) compiles and links for the ESP32-S3.
5. Integration on hardware with the checklist of section 11.
6. Later: UDP, `reload`, TLS option, X-Forwarded-For injection, Windows service wrapper, OTA via the host.
