#!/usr/bin/env bash
set -Eeuo pipefail

PARENT_MOUNT="${ASSET_HUB_LIBRARY_PARENT_MOUNT:-/mnt/hdd}"
LIBRARY_ROOT="${ASSET_HUB_LIBRARY_ROOT:-/home/resourse}"

log() { printf '[library-mount] %s\n' "$*"; }
mounted_ok() {
  mountpoint -q "$PARENT_MOUNT" && mountpoint -q "$LIBRARY_ROOT" &&
    find "$LIBRARY_ROOT" -mindepth 1 -maxdepth 2 -print -quit | grep -q .
}

if mounted_ok; then
  log "already mounted: $LIBRARY_ROOT"
  exit 0
fi

device="$(findmnt --fstab --evaluate -n -o SOURCE --target "$PARENT_MOUNT")"
[[ -b "$device" ]] || { log "invalid block device for $PARENT_MOUNT: $device"; exit 1; }

systemctl reset-failed mnt-hdd.mount home-resourse.mount || true
if systemctl start mnt-hdd.mount && systemctl start home-resourse.mount && mounted_ok; then
  log "mounted normally: $device -> $LIBRARY_ROOT"
  exit 0
fi

if findmnt -rn -S "$device" >/dev/null; then
  log "refusing repair because $device is already mounted"
  exit 1
fi
fstype="$(blkid -s TYPE -o value "$device")"
[[ "$fstype" == "ntfs" ]] || { log "refusing repair for non-NTFS device: $fstype"; exit 1; }

kernel_name="$(basename "$(readlink -f "$device")")"
if ! journalctl -k -b --no-pager | grep -Fq "ntfs3(${kernel_name}): volume is dirty"; then
  log "mount failed for a reason other than an NTFS dirty volume; manual inspection required"
  exit 1
fi

log "repairing dirty NTFS metadata on exact device $device"
ntfsfix "$device"
ntfsfix -d "$device"

systemctl reset-failed mnt-hdd.mount home-resourse.mount || true
systemctl start mnt-hdd.mount
systemctl start home-resourse.mount
mounted_ok || { log "mount verification failed after ntfsfix"; exit 1; }
log "recovered: $device -> $LIBRARY_ROOT"
