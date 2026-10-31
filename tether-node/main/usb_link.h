#pragma once
#include <stddef.h>
#include <stdint.h>

int usb_link_init(void);                       /* installs TinyUSB, brings up the CDC-ACM port; returns 0 or -1 */
int usb_link_fd(void);                         /* eventfd that becomes readable when bytes arrived */
int usb_link_read(uint8_t *buf, size_t cap);   /* >0 bytes, 0 none */
int usb_link_write(const uint8_t *buf, size_t n, int may_drop);   /* 0 ok/dropped, <0 stalled */
