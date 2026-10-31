# serial-lan-bridge

Wi-Fi clients reach an HTTP API on a Windows PC's network through an ESP32-S3 over a plain USB serial (COM) port.
No drivers, no network adapter on the PC.

| Component | Dir | Role |
|---|---|---|
| **tether-node** | `tether-node/` | ESP32-S3 firmware: Wi-Fi listeners, USB serial link |
| **tether-host** | `tether-host/` | Python on the bridge PC: connects to the API, serial mux |

Design: [ARCHITECTURE.md](ARCHITECTURE.md). Tests: `python tether-host/tests/test_protocol.py`.
