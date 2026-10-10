#!/bin/sh
# Flash a raw UV-K5 firmware image with k5prog, refusing anything that looks unsafe.
#
# Preconditions (checked):
#   * an EEPROM backup taken in normal mode exists in ~/influ/radio/eeprom_backup_*.raw
#   * the radio is in bootloader mode (PTT held while switching on: torch LED lit, screen blank)
#   * its bootloader is a DP32G030 one (2.00.06 / 5.00.01); 7.x = UV-K5 V3 / K1 (other MCU) -> abort
# The flash only replaces firmware; calibration lives in EEPROM, which is not touched.
#
# usage: flash_k5.sh ~/influ/radio/fw/cec_0.3VB.raw
set -eu
FW=${1:?raw firmware image}
K5=$HOME/influ/tools/k5prog/k5prog
PORT=/dev/ttyUSB0

ls "$HOME"/influ/radio/eeprom_backup_*.raw >/dev/null 2>&1 || {
  echo "no EEPROM backup yet: back up in normal mode first" >&2; exit 2; }
size=$(stat -c %s "$FW")
[ "$size" -le 61440 ] || { echo "image too large ($size > 61440)" >&2; exit 2; }

echo "waiting for the bootloader (radio must be in flash mode)..."
ver=$(timeout 20 "$K5" -p "$PORT" -D -v 2>&1 | tee /dev/stderr | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
case "$ver" in
  2.00.06|5.00.01) echo "bootloader $ver: DP32G030, compatible" ;;
  "") echo "no bootloader message: is the radio in flash mode?" >&2; exit 3 ;;
  *) echo "bootloader $ver is not a known DP32G030 bootloader - refusing to flash" >&2; exit 4 ;;
esac

echo "flashing $FW ($size bytes)"
"$K5" -p "$PORT" -F -YYY -b "$FW"
echo "done: switch the radio off and on again (without PTT)"
