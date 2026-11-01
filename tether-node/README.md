# tether-node

ESP32-S3 firmware (ESP-IDF, C; verified to build with ESP-IDF 6.1.0 through PlatformIO). Specification: [../ARCHITECTURE.md](../ARCHITECTURE.md) sections 5 and 7.

```
components/tether_core/   portable C, no ESP-IDF dependency: framing, JSON, config validation, the engine
                          (listeners, channels, flow control, control protocol). Builds and is tested on Linux.
main/                     ESP32 glue: usb_link (TinyUSB CDC-ACM), wifi_mgr (station, hotspot, mDNS), platform_esp, app_main
sim/                      Linux simulator: the same engine behind a pseudo-terminal (used by the host's end-to-end tests)
tests/test_core.c         C unit tests; shared byte vectors in ../testvectors/frames.json
```

## Test on a PC (no hardware)

```
make -C sim test          # C unit tests under ASan/UBSan
make -C sim               # builds sim/build/tether_sim
python -m unittest discover -s ../tether-host/tests     # includes end-to-end tests against tether_sim
```

## Build and flash the ESP32-S3

```
pio run
# hold BOOT while plugging in the USB cable (or press RESET with BOOT held), then:
pio run -t upload --upload-port <port>
# plain ESP-IDF (same sources; not exercised yet): idf.py set-target esp32s3 && idf.py build && idf.py -p <port> flash
```

After the first flash, `tether-host bootloader` puts a running node back into download mode. Console output goes to UART0.
Nothing is stored in flash apart from the firmware; configuration arrives from `tether-host` on every start.
