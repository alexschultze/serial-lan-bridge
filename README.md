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
| Wire protocol (framing, message payloads), shared test vectors, unit tests | **done** (`tether-host/tether_host/protocol.py`, `testvectors/`) |
| tether-host (config, serial link, session, bridge, CLI) | planned, specified in ARCHITECTURE.md sections 6 and 8 |
| tether-node firmware | planned, specified in sections 5 and 7 |
| Hardware acceptance tests | planned, section 11 |

The commands below describe the intended interface; only the tests run today.

## Requirements

* ESP32-S3 board **whose USB port is the chip's native USB** (GPIO19/20), not a CP210x/CH340 UART bridge. In Windows Device
  Manager the board should report hardware ID `VID_303A`. (Boards with a second "UART" port: use the one labelled USB.)
* Bridge PC: Windows 11 (Linux/macOS also work), Python 3.11+, `pip install pyserial` (a library, no driver).
* Network B with DHCP and WPA2/WPA3 (2.4 GHz). The API reachable from the PC.
* To build the firmware: ESP-IDF 5.x on Linux or Windows.

## Quick start (target workflow)

1. **Flash the node** (once; Linux or Windows):
   ```
   cd tether-node
   idf.py set-target esp32s3
   idf.py build
   # put the board in download mode (hold BOOT while plugging in), then:
   idf.py -p <port> flash
   ```
2. **Configure**: copy `tether.example.toml` to `tether.toml` and set the Wi-Fi network, the hotspot (optional) and the
   listeners (`port` on the node, `target` of the API).
3. **Run on the PC**:
   ```
   pip install ./tether-host
   tether-host check-config --config tether.toml
   tether-host run --config tether.toml
   ```
4. **Use it**: from a Wi-Fi client, point the API base URL at the node, for example
   `http://tether-a1b2.local:8000/v1` (the name and current IP are shown by `tether-host status`).

Other commands: `tether-host ports` (list serial ports), `tether-host status`, `tether-host bootloader` (reboot the node
into download mode for re-flashing).

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
python -m unittest discover -s tether-host/tests
```

`testvectors/frames.json` holds byte-exact frames that both the Python and the (future) C implementation must reproduce;
regenerate it with `testvectors/make_vectors.py` only when the wire format changes (bump the protocol version).
