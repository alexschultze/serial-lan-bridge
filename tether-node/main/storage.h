#pragma once

/* 1 MB FAT volume in the `storage` flash partition, offered to the PC as a USB drive.
 * While the PC has the device enumerated the drive belongs to the PC; otherwise it is mounted at STORAGE_PATH for the firmware.
 * Must be called before tinyusb_driver_install(). Returns 0, or -1 (the serial link still works without the drive). */
#define STORAGE_PATH "/data"

int storage_init(void);
const char *storage_owner(void);   /* "pc", "node" or "off" */
