# serial-lan-bridge

Let clients on a **Wi-Fi network** reach an **HTTP API on a Windows PC's network** (for example an OpenAI-compatible
completions server, including streaming), through an **ESP32-S3** connected to the PC by a single USB cable.

* **No drivers on the PC.** The ESP32-S3 appears as a plain USB serial (COM) port that Windows 11 handles itself.
* **No network adapter on the PC.** Translation happens per connection (a NAT at connection level): the API sees only the
  PC; the Wi-Fi network sees only the ESP32.
* **One TOML file** configures Wi-Fi, an optional fallback hotspot and the port mappings, and is applied on every start.
  The ESP32 stores no configuration and no passwords.

```
  Wi-Fi clients ──HTTP──► [ tether-node ]══ USB serial ══[ tether-host ]──HTTP──► API server
                           ESP32-S3                       Python on Windows 11
```

| Component | Dir | Role |
|---|---|---|
| **tether-node** | `tether-node/` | ESP32-S3 firmware (ESP-IDF, C): joins Wi-Fi by DHCP (or starts a hotspot), listens on ports, forwards connections over USB |
| **tether-host** | `tether-host/` | Python 3.11+ on the bridge PC: pushes the configuration, connects to the API, multiplexes connections over the serial port |

Full design, wire protocol, state machines and decisions: **[ARCHITECTURE.md](ARCHITECTURE.md)**.

## Status

| Part | State |
|---|---|
| Wire protocol, shared byte-exact test vectors | done, tested in Python and C |
| **tether-host** (config, serial link with reconnect, session, bridge channels, CLI) | done, tested against a Python node and against the real C engine over a pseudo-terminal |
| **tether-node** portable core (framing, JSON, config validation, listeners, channels, flow control, control protocol) | done, C unit tests under ASan/UBSan, plus a Linux simulator that runs the same engine |
| **tether-node** ESP32 glue (TinyUSB CDC, Wi-Fi station/hotspot, mDNS) | compiles and links for the ESP32-S3 without warnings (ESP-IDF 6.1.0 via PlatformIO; 806 KB of a 3 MB slot, 15% RAM); **not yet run on a board** |
| Hardware acceptance tests (ARCHITECTURE.md section 11) | not yet run: they need a board and the Windows PC |

Tests today: 32 Python tests, 193 C checks. Measured over a pseudo-terminal (no USB): about 1.7 MB/s of combined throughput; the
USB full-speed link is expected to cap this at 0.8 to 1 MB/s.

## Requirements

* ESP32-S3 board **whose USB port is the chip's native USB** (GPIO19/20), not a CP210x/CH340 UART bridge. In Windows Device
  Manager the board should report hardware ID `VID_303A`. (Boards with a second "UART" port: use the one labelled USB.)
* Bridge PC: Windows 11 (Linux/macOS also work), Python 3.11+, `pip install pyserial` (a library, no driver).
* Network B with DHCP and WPA2/WPA3 (2.4 GHz). The API reachable from the PC.
* To build the firmware: PlatformIO (`pip install platformio`) or ESP-IDF 5.2 or newer, on Linux or Windows.

## Quick start

1. **Build and flash the node** (once; Linux or Windows). With PlatformIO (`pip install platformio`), the toolchain the firmware is
   verified to build with:
   ```
   cd tether-node
   pio run
   # put the board in download mode (hold BOOT while plugging in the USB cable), then:
   pio run -t upload --upload-port <port>
   ```
   With plain ESP-IDF (same sources, `CMakeLists.txt`; not exercised yet): `idf.py set-target esp32s3 build flash -p <port>`.
2. **Configure**: copy `tether.example.toml` to `tether.toml`; set the Wi-Fi network, the optional hotspot and the
   listeners (`port` opened on the node, `target` = the API as seen from the PC).
3. **Run on the PC**:
   ```
   pip install ./tether-host
   tether-host check-config --config tether.toml
   tether-host run --config tether.toml
   ```
4. **Use it**: from a Wi-Fi client, set the API base URL to the node, for example `http://tether-a1b2.local:8000/v1`
   (`tether-host status` shows the name and the current IP address).

Other commands: `tether-host ports` (lists serial ports and marks tether nodes), `tether-host status`,
`tether-host bootloader` (restarts the node into download mode for re-flashing).

**Try it without hardware** (Linux/macOS): `make -C tether-node/sim` builds a simulated node; it prints a pseudo-terminal path
(`PTY /dev/pts/N`) that you can set as `host.port` in the config.

## Configuration at a glance

```toml
[wifi]
country = "DE"
[[wifi.network]]
ssid = "NetworkB"
psk  = "..."

[hotspot]                  # optional fallback, WPA2 only; remove the section to never start one
psk = "..."

[[listener]]
id     = 1
port   = 8000                  # opened on the node
target = "10.0.0.5:8000"       # where the PC connects
```

All options with comments: [tether.example.toml](tether.example.toml); rules and semantics: ARCHITECTURE.md section 6.

## Behaviour and limits

* TCP/HTTP only in v1; no ping, broadcast or UDP. Streaming responses are forwarded without buffering.
* Throughput about 0.8 to 1 MB/s in total (USB full speed), +2 to 5 ms per chunk; up to 16 simultaneous connections, 4 listeners.
* When the PC or `tether-host` is not running, the node does nothing useful; when it is overloaded, clients get HTTP 503.
* **No TLS between clients and the node:** requests (including API keys) cross the Wi-Fi network in clear text. Use a
  network you trust. The API's own authentication keeps working because requests are not modified.
* The API sees every request as coming from the PC, not from the real client.
* Passwords are stored in the TOML file on the PC: restrict its permissions.

## Tests

```
make -C tether-node/sim test                       # C unit tests (ASan/UBSan)
python -m unittest discover -s tether-host/tests   # protocol, host with a Python node, host with the C engine (needs make and a C compiler)
```

`testvectors/frames.json` holds byte-exact frames that both the Python and the C implementation reproduce;
regenerate it with `testvectors/make_vectors.py` only when the wire format changes (bump the protocol version).
