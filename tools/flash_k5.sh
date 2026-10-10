#!/bin/sh
# Flash a raw UV-K5 firmware image with k5prog, refusing anything that looks unsafe.
#
#   flash_k5.sh check        radio in flash mode -> read and record the bootloader version
#   (switch the radio off, then PTT + power on again: a k5prog session that only listened
#    leaves the bootloader unable to confirm the next session's writes)
#   flash_k5.sh flash FW     flash, only if `check` saw a DP32G030 bootloader in the last 15 min
#
# Also requires an EEPROM backup (k5prog -r in normal mode) in ~/influ/radio/eeprom_backup_*.raw.
# Bootloaders 2.00.06 / 5.00.01 are DP32G030; 7.x = UV-K5 V3 / K1 with another MCU -> refused.
# Needs k5prog with tools/k5prog-0518.patch (bootloaders that broadcast a different UID).
# The flash replaces firmware only; calibration lives in EEPROM, which is not touched.
set -eu
K5=$HOME/influ/tools/k5prog/k5prog
PORT=/dev/ttyUSB0
STATE=$HOME/influ/radio/bootloader_check

case "${1:-}" in
  check)
    ver=$(timeout 20 "$K5" -p "$PORT" -D -Y -v 2>&1 | grep -oE 'Flasher version is: \[[0-9.]+\]' \
          | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
    case "$ver" in
      2.00.06|5.00.01) echo "$ver" > "$STATE"
        echo "bootloader $ver: DP32G030, compatible. Now power-cycle into flash mode again, then: flash" ;;
      "") echo "no bootloader message: is the radio in flash mode?" >&2; exit 3 ;;
      *) echo "bootloader $ver is not a known DP32G030 bootloader - do not flash" >&2; exit 4 ;;
    esac
    ;;
  flash)
    FW=${2:?raw firmware image}
    ls "$HOME"/influ/radio/eeprom_backup_*.raw >/dev/null 2>&1 || {
      echo "no EEPROM backup yet: back up in normal mode first" >&2; exit 2; }
    [ -n "$(find "$STATE" -mmin -15 2>/dev/null)" ] || {
      echo "run 'check' first (within 15 min) to verify the bootloader" >&2; exit 2; }
    size=$(stat -c %s "$FW")
    [ "$size" -le 61440 ] || { echo "image too large ($size > 61440)" >&2; exit 2; }
    echo "flashing $FW ($size bytes), bootloader $(cat "$STATE")"
    out=$("$K5" -p "$PORT" -F -YYY -b "$FW" 2>&1) || true
    printf '%s\n' "$out" | grep -E "FLASH at|Flasher version|ERROR|confirmation" | tail -4
    if printf '%s\n' "$out" | grep -qE "ERROR|Stopping flash"; then
      echo "FLASH FAILED - power-cycle into flash mode (PTT + power on) and run flash again" >&2
      exit 5
    fi
    rm -f "$STATE"
    echo "done: $(printf '%s\n' "$out" | grep -c 'result=1') blocks written." \
         "Switch the radio off and on again (without PTT)"
    ;;
  *) sed -n '2,13p' "$0" >&2; exit 2 ;;
esac
