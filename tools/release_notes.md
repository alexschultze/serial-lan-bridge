## Files

| File | What it is |
|---|---|
| `tether-node-@VERSION@-esp32s3-n16r8.bin` | firmware for an ESP32-S3-WROOM-1 **N16R8** board (16 MB flash, 8 MB PSRAM), one image flashed at `0x0` |
| `tether-host-@VERSION@.zip` | tether-host (Python 3.11+) with the configuration template `tether.example.toml` |
| `SHA256SUMS.txt` | checksums of both |

## Flash the node (Windows or Linux, no driver needed)

1. `pip install esptool`
2. Put the board in download mode: hold **BOOT** while plugging the cable into the native **USB** port (a node that already runs
   tether firmware can do this itself: `tether-host bootloader`). It appears as a new COM port.
3. `esptool --chip esp32s3 --port COM5 write-flash 0x0 tether-node-@VERSION@-esp32s3-n16r8.bin` (your COM port)
4. Press **RESET** (or unplug and plug in again). The board now shows up as a COM port and a 1 MB USB drive.

Files on the USB drive are kept: the image does not cover the drive's partition. Do **not** run `esptool erase-flash`, which
deletes them.

## Install and run tether-host

1. Unpack `tether-host-@VERSION@.zip` and open a terminal in the folder.
2. `pip install .`
3. Copy `tether.example.toml` to `tether.toml` and replace every `<...>` value; `tether-host check-config --config tether.toml` tells
   you what is still missing.
4. `tether-host run --config tether.toml`

Use the firmware and tether-host of the same release; tether-host warns when they differ.
