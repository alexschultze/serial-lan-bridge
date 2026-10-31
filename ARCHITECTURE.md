# Architecture and specification

Status: v1 implemented and tested without hardware (Python host, portable C node core, Linux simulator); the ESP32 glue
(USB, Wi-Fi) is written but not yet run on a board (see README). This document is the
single source of truth; where code and text disagree, fix one of them. Items marked **(verify)** depend on
vendor behaviour that must be confirmed during implementation.

## 1. Purpose and requirements

Clients on a Wi-Fi network (**network B**) must reach an HTTP API (OpenAI-style completions, including streaming
SSE) that lives on the network of a Windows 11 PC (**network A**). The only link between the two is a USB cable
between the PC and an ESP32-S3. Hard requirements, all of which shaped the design:

| # | Requirement | Consequence |
|---|---|---|
| R1 | Windows 11 PC, **no additional drivers** | ESP32-S3 uses its native USB as **CDC-ACM** (inbox `usbser.sys`). Python uses `pyserial` (a library, not a driver). |
| R2 | The device must **not appear as a network adapter** in Windows | No USB Ethernet/RNDIS/NCM. No IP interface on the PC for the bridge, so no packet-level NAT or L2 bridge is possible. |
| R3 | Behaves like a **NAT** (neither network learns the other's addresses) | Connection-level NAT: destination NAT on the node (listener port to target) and source NAT (the API sees the PC; network B sees the node). |
| R4 | The ESP32 **joins a network by DHCP**, and can **advertise a hotspot** when none is found | Wi-Fi STA with AP fallback, section 7. |
| R5 | Primary traffic: **plain HTTP, including streaming** | No HTTP parsing, no buffering, low per-chunk latency; TCP only in v1. |
| R6 | Bandwidth below 1 MB/s is acceptable | USB full speed (12 Mbit/s) is sufficient. |
| R7 | **One TOML file** configures everything and is applied on **every (re)start** | Node holds no persistent configuration and no secrets in flash, section 6. |
| R8 | Flash from Linux or Windows | Standard ESP-IDF tooling, section 12. |

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
  Not found, hotspot configured ──► node starts its own hotspot
```

**Roles.** The *node* owns everything on the Wi-Fi side: Wi-Fi, listeners, accepting client connections, the
allow-list. The *host* owns everything on the PC side: configuration, the target addresses, connecting to the API,
status display. The node never learns the targets (less information on the exposed device); the host never touches
Wi-Fi directly.

**NAT semantics.**
* Destination NAT: `node_wifi_ip:listener_port` maps to `target_host:target_port`, held in the host's config.
* Source NAT: the API sees only connections from the PC. Network B sees only the node's Wi-Fi address (DHCP, not
  known in advance; found via mDNS and `tether-host status`).
* Connection-level only: TCP in v1. No ICMP/ping, broadcast or multicast crosses the bridge, and UDP is reserved
  for a later version (message range `0x40-0x4F`).

## 3. Components and repository layout

```
serial-lan-bridge/
├── README.md
├── ARCHITECTURE.md              this file
├── tether.example.toml          annotated example configuration
├── testvectors/                 frames.json (shared by Python and C tests), make_vectors.py
├── tether-host/                 Python 3.11+, depends on pyserial only
│   ├── pyproject.toml           entry point `tether-host`, `python -m tether_host`
│   ├── tether_host/
│   │   ├── protocol.py          framing and payload helpers (reference implementation)
│   │   ├── config.py            load/validate TOML, build the node CONFIG JSON
│   │   ├── link.py              Transport: pyserial (reader/writer threads) and in-memory loopback; port discovery
│   │   ├── session.py           handshake, PING/PONG, CONFIG push, STATUS, channel table, reconnect
│   │   ├── bridge.py            per-channel pipe to the target, flow control, half-close, 502/504
│   │   └── cli.py               run | status | ports | check-config | bootloader
│   └── tests/                   protocol, fake_node.py + test_e2e.py (Python node), test_sim_e2e.py (C engine)
└── tether-node/                 ESP-IDF 5.x project (C)
    ├── CMakeLists.txt, sdkconfig.defaults
    ├── components/tether_core/  portable C without ESP-IDF dependencies: tc_proto (COBS, CRC, deframer),
    │                            tc_json, tc_config (parse/validate), tc_engine (listeners, channels, flow control, control protocol)
    ├── main/                    ESP32 glue: usb_link (TinyUSB CDC-ACM), wifi_mgr (station/hotspot/mDNS), platform_esp, app_main
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

* `chan = 0` is the control channel. `chan 1..16` are data channels, one per client connection.
* Channel ids are allocated by the **node** (lowest free id), because connections originate on the Wi-Fi side. An
  id is reusable only after the channel has ended (4.6).

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
| 0x30 | DATA | both | n | stream bytes, 1..1024 |
| 0x31 | CREDIT | both | n | additional bytes the peer may send, u32 |
| 0x32 | CLOSE | both | n | reason u8: 0 EOF, 1 RESET, 2 PROTO, 3 NO_CHANNEL |

Unknown types on chan 0 are ignored (forward compatibility). A frame for a channel that is not open is dropped
and, unless it is itself a CLOSE, answered with `CLOSE(NO_CHANNEL)`.

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
  "link": { "window": 4096, "never_used_timeout": 30, "idle_timeout": 0 }
}
```

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

ESP-IDF 5.x (C), `idf.py`; components `esp_tinyusb` (managed component), `esp_wifi`, `esp_netif`, `lwip`, `mdns`,
`json` (cJSON), `esp_timer`. Arduino, ESPHome and MicroPython were rejected (section 13). Key `sdkconfig.defaults`:

* TinyUSB device with a single CDC-ACM interface, custom descriptors: VID/PID of `esp_tinyusb` defaults
  (`0x303A`/`0x4001` **(verify)**), product string `tether-node`, serial number = base MAC. TX/RX buffers ≥ 2048.
* Console: UART0 (or disabled), **not** USB. ESP log output is additionally sent as `LOG` frames while a host is attached.
* `CONFIG_LWIP_MAX_SOCKETS` ≥ 24 (16 channels + 4 listeners + wake-up fd + margin; check the maximum of the chosen
  IDF version **(verify)**); TCP send/receive buffers and window raised toward 16 KiB (start from the IDF iperf example).
* Wi-Fi power save off (`WIFI_PS_NONE`); task watchdog on; `PANIC_REBOOT` (a crash yields a new `session`, which the host detects).
* No persistent configuration: NVS is initialised only because the Wi-Fi driver calibration data needs it.

### 5.2 Tasks

| Task | Role |
|---|---|
| TinyUSB task / `rx_cb` | copies received CDC bytes into a stream buffer and signals an eventfd that the `net` task selects on |
| `net` | the engine (`tc_engine_poll`): one `select()` loop over the eventfd, listeners and channel sockets; owns the channel table (no locks); parses frames, applies the credit rules, runs the timers; it is also the only writer to CDC, so frames never interleave, and a write that makes no progress for 5 s counts as a stall (4.8) |
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
* **No other network services.** The node listens only on the configured ports; there is no web UI, OTA or debug server.

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
```

### 6.2 Validation (host side, before anything is sent; `tether-host check-config` runs only this)

Required: at least one `[[wifi.network]]` and one `[[listener]]`. SSID 1..32 bytes, PSK 8..63 characters (or absent),
hotspot PSK 8..63; at most 4 networks and 4 listeners; unique `id` and `port`; `target` is `host:port`; `allow`
entries are valid IPv4 CIDRs; `window` in range. The node validates again and reports problems in CONFIG_RES.

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
  socket and channels are closed; new → opened.
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
* **Serial transport:** discovery through `serial.tools.list_ports` (VID `0x303A`, not the ROM bootloader PID `0x1001`, and product containing `tether` or PID `0x4001`; or the
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
| All 16 channels used | static 503 | – | HTTP 503 |
| CRC/framing error | resets session (4.8) | resets session | in-flight connections reset |
| Config invalid | keeps previous config, reports errors | prints the errors | – |
| Two nodes plugged in, no selector | – | refuses to start, lists ports | – |

In-flight streaming responses cannot be resumed after a fault; clients retry.

## 10. Security

* Threat model: any device on network B (or the hotspot) can try to reach the listeners; the USB link is physically trusted.
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
| Unit (Python) | framing, payload helpers, chunked CONFIG | `tether-host/tests/test_protocol.py` |
| Shared vectors | Python and C decode and re-encode identical bytes | `testvectors/frames.json`; `make_vectors.py` only with a protocol version bump |
| Unit (C) | CRC, COBS, deframer faults, JSON, config validation, vectors; built with ASan/UBSan | `make -C tether-node/sim test` |
| Host against a Python node | streaming without buffering, 1.5 MB round trip, half-close, 502, 503 at 17 connections, node reset, framing fault, silent node | `tether-host/tests/test_e2e.py` with `fake_node.py` |
| Host against the real C engine | same over a pseudo-terminal and the real `SerialTransport`: streaming, transfers with 4 KiB and 1 KiB windows, half-close, 502/503, allow-list, never-used timeout, node killed and restarted, rejected Wi-Fi config | `tether-host/tests/test_sim_e2e.py` (needs `make` and a C compiler; Linux/macOS) |
| Hardware | checklist below | a board plus the bridge PC |

Hardware acceptance checklist: (1) fresh Windows 11 without added drivers: board enumerates as one COM port, "USB Serial Device",
no driver prompt; (2) `tether-host run` configures the node; (3) the OpenAI SDK streams a chat completion from a Wi-Fi client to
the API, token by token; (4) 16 parallel requests, the 17th gets 503; (5) 100 MB transfer: sustained ≥ 0.8 MB/s; (6) unplug/replug
during a stream: recovery without restarting the host; (7) host killed: node answers 503 within 10 s; (8) Wi-Fi AP powered
off then on: reconnect; (9) unknown SSID and `[hotspot]` set: hotspot appears after `fallback`, client reaches the API through it;
(10) 24 h soak with periodic requests: no heap growth in STATUS.

CI (`.github/workflows/ci.yml`): Python and C tests on Linux; a second job builds the firmware with ESP-IDF and uploads the
image as an artifact.

## 12. Build, flash and operate

* **Build node:** ESP-IDF 5.x environment, `cd tether-node && idf.py set-target esp32s3 && idf.py build`.
* **First flash** (Linux or Windows): put the board in ROM download mode (hold BOOT, plug in USB or tap RESET). The chip's
  ROM enumerates as a USB serial device that Windows binds with its inbox driver, `idf.py -p <port> flash`
  (or `esptool.py`). After the app boots, it re-enumerates as `tether-node` on a (possibly different) COM port.
* **Re-flash:** the application's USB port does not support esptool's automatic reset, so run `tether-host bootloader`
  (sends REBOOT mode 1) or repeat the manual BOOT procedure.
* **Which port on the board:** the one wired to the chip's native USB (GPIO19/20). Ports behind a CP210x/CH340 UART
  bridge are not usable (driver required, ≈ 0.3 MB/s). Single-port S3 boards are normally native USB; check that Device
  Manager shows VID `303A`.
* **Debugging:** logs over UART0 and as `LOG` frames. The chip's built-in JTAG shares the USB pins with TinyUSB, so source-level
  debugging needs an external JTAG adapter or a development build that uses the built-in USB-Serial/JTAG instead of TinyUSB.
* **Updates:** USB flashing only (no OTA, by design: no network services on the node).

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
| Node config scope | targets stay on the PC | targets on the node (exposes internal addresses on a Wi-Fi device) |
| Framework | ESP-IDF 5.x (C) | Arduino-ESP32 (less control of USB/lwIP), ESPHome (build-time config, own services, USB device support unclear; custom component would carry the whole product anyway), MicroPython (speed, memory, REPL conflicts), Rust (tooling cost) |
| Host language | Python 3.11+, pyserial only | other languages: Python is the PC environment given by the requirement |
| Hotspot | optional, WPA2 only, off unless configured | always-on or open AP (exposes the API) |
| Transport security | none in v1 (documented) | TLS on the ESP32 (RAM, certificates, complexity); possible later |

## 14. Open points to verify during implementation

1. `esp_tinyusb` default VID/PID and string-descriptor customisation in the chosen IDF release; Windows enumerates it as a plain
   "USB Serial Device" with no driver prompt.
2. `CONFIG_LWIP_MAX_SOCKETS` upper limit and RAM budget with 16 channels plus Wi-Fi AP+STA.
3. Real USB CDC throughput with `pyserial` on Windows 11 (target ≥ 0.8 MB/s) and the best CDC buffer sizes.
4. Software reset into ROM download mode on the S3 (REBOOT mode 1) from running TinyUSB firmware.
5. AP+STA behaviour (channel following, scan interruptions) with real access points.
6. The node uses its own CRC-32 (`tc_crc32`, verified against the vectors), so no ESP-ROM function is needed.
7. The ESP32 glue (`usb_link.c`, `wifi_mgr.c`, `platform_esp.c`) follows the esp_tinyusb 1.x and ESP-IDF 5.x APIs from documentation;
   it must be compiled and run on a board before it can be called verified.

## 15. Roadmap

1. Done: framing, payload helpers, shared test vectors, unit tests.
2. Done: tether-host (config, transports, session, bridge, CLI) with end-to-end tests against a Python node and the C engine.
3. Done: portable node core in C (protocol, JSON, config, engine) with unit tests and the Linux simulator.
4. Written, build and run to be confirmed: ESP32 glue (USB CDC, Wi-Fi station/hotspot/mDNS, platform hooks).
5. Integration on hardware with the checklist of section 11.
6. Later: UDP, `reload`, TLS option, X-Forwarded-For injection, Windows service wrapper, OTA via the host.
