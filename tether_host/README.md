# tether-host

The PC side of serial-lan-bridge: talks to a tether-node (ESP32-S3) over its USB serial port, sends it the configuration,
connects Wi-Fi clients to servers on the PC's network and carries local ports to endpoints on the Wi-Fi network.

## Install

Python 3.11 or newer. In this folder:

```
pip install .
```

This installs the `tether-host` command and its one dependency, `pyserial`. To run without installing:
`pip install pyserial`, then `python -m tether_host ...` from this folder.

## Configure and run

1. Copy `tether.example.toml` to `tether.toml` and replace every `<...>` value. The comments in the file explain each setting.
2. `tether-host check-config --config tether.toml`: lists anything that is missing or wrong.
3. `tether-host run --config tether.toml`

Other commands: `tether-host status` (in a second terminal: Wi-Fi state, the node's address, counters, the USB drive),
`tether-host ports` (serial ports, the node is marked), `tether-host bootloader` (restart the node into download mode for flashing).
Add `--log-level debug` for detailed output.

The firmware for the node is a separate download of the same release; tether-host warns when the two versions differ.
