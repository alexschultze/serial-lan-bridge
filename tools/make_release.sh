#!/usr/bin/env bash
# Build the release assets into dist/:
#   tether-node-<version>-esp32s3-n16r8.bin   merged firmware image, flashed at 0x0 (bootloader, partition table, app)
#   tether-host-<version>.zip                 tether-host sources + the configuration template
#   SHA256SUMS.txt
# and build/RELEASE_NOTES.md. Usage: tools/make_release.sh <version>, e.g. 0.2.0 (the release workflow passes the tag
# without its leading "v"). Needs: python3, platformio (pio), esptool.
set -euo pipefail

VERSION="${1:?usage: tools/make_release.sh <version>  (e.g. 0.2.0)}"
# 1.2.3, or a pre-release 1.2.3-a1 / -b1 / -rc1: valid for both the firmware string and Python packaging (PEP 440)
if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-(a|b|rc)[0-9]+)?$ ]]; then
    echo "version must look like 1.2.3 or 1.2.3-rc1 (also -a1, -b1), got '$VERSION'" >&2
    exit 1
fi
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/dist"
BUILD="$ROOT/build"
rm -rf "$DIST" "$BUILD"
mkdir -p "$DIST" "$BUILD"

# ---- firmware: clean build, so the version is compiled in -------------------------------------------------------
(
    cd "$ROOT/tether-node"
    rm -rf .pio/build sdkconfig.esp32s3
    TETHER_VERSION="$VERSION" pio run
)
B="$ROOT/tether-node/.pio/build/esp32s3"
SDK="$ROOT/tether-node/sdkconfig.esp32s3"
boot_off=$(sed -n 's/^CONFIG_BOOTLOADER_OFFSET_IN_FLASH=//p' "$SDK")
part_off=$(sed -n 's/^CONFIG_PARTITION_TABLE_OFFSET=//p' "$SDK")
app_off=$(python3 - "$ROOT/tether-node/partitions.csv" <<'PY'
import csv, sys
for row in csv.reader(l for l in open(sys.argv[1]) if l.strip() and not l.lstrip().startswith("#")):
    if row[1].strip() == "app":
        print(row[3].strip()); break
PY
)
FW="$DIST/tether-node-$VERSION-esp32s3-n16r8.bin"
# The image ends with the application: the `storage` partition (the USB drive) is not part of it and keeps its files.
# Flash settings are kept exactly as the build wrote them: ESP-IDF puts DIO into the bootloader header even when QIO is
# configured (the ROM loads the bootloader in DIO, the bootloader then switches to QIO). Forcing QIO here makes the ROM
# fail to load the bootloader (`ets_loader.c 78`) and the chip resets in a loop.
esptool --chip esp32s3 merge-bin --flash-mode keep --flash-freq keep --flash-size keep -o "$FW" \
    "$boot_off" "$B/bootloader.bin" "$part_off" "$B/partitions.bin" "$app_off" "$B/firmware.bin"
python3 - "$B/bootloader.bin" "$FW" <<'PY'
import sys
built, merged = (open(p, "rb").read(4) for p in sys.argv[1:])
if built != merged:
    sys.exit(f"merged image header {merged.hex()} differs from the built bootloader's {built.hex()}")
print(f"image header {merged.hex()} matches the built bootloader (flash mode {['QIO', 'QOUT', 'DIO', 'DOUT'][merged[2]]})")
PY

# ---- tether-host source zip ----------------------------------------------------------------------------------------
STAGE="$BUILD/tether-host-$VERSION"
mkdir -p "$STAGE"
cp -r "$ROOT/tether_host/tether_host" "$ROOT/tether_host/pyproject.toml" "$ROOT/tether_host/README.md" "$STAGE/"
cp "$ROOT/tether.example.toml" "$STAGE/"
find "$STAGE" -name __pycache__ -prune -exec rm -rf {} +
python3 - "$STAGE/tether_host/__init__.py" "$VERSION" <<'PY'
import re, sys
path, version = sys.argv[1:]
src = open(path).read()
new = re.sub(r'^__version__ = ".*"', f'__version__ = "{version}"', src, count=1, flags=re.M)
assert new != src or f'"{version}"' in src, "could not stamp the version"
open(path, "w").write(new)
PY
(cd "$BUILD" && python3 -m zipfile -c "$DIST/tether-host-$VERSION.zip" "tether-host-$VERSION")

# ---- checksums and release notes -----------------------------------------------------------------------------------
(cd "$DIST" && sha256sum -- *.bin *.zip > SHA256SUMS.txt)
sed "s/@VERSION@/$VERSION/g" "$ROOT/tools/release_notes.md" > "$BUILD/RELEASE_NOTES.md"
echo "release $VERSION assets:"
ls -l "$DIST"
