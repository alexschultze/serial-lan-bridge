/* CDC-ACM link to tether-host over the native USB port (esp_tinyusb 1.x API; verify against the installed version). */
#include "usb_link.h"

#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#include "esp_log.h"
#include "esp_mac.h"
#include "esp_timer.h"
#include "esp_vfs_eventfd.h"
#include "freertos/FreeRTOS.h"
#include "freertos/stream_buffer.h"
#include "tinyusb.h"
#include "tusb_cdc_acm.h"

static const char *TAG = "usb";
static StreamBufferHandle_t rx_sb;
static int evfd = -1;
static char serial[20];
static const char lang_id[] = {0x09, 0x04};   /* language ID: English (US) */

static void rx_cb(int itf, cdcacm_event_t *event)
{
    uint8_t buf[256];
    size_t n = 0;
    do {
        n = 0;
        if (tinyusb_cdcacm_read((tinyusb_cdcacm_itf_t)itf, buf, sizeof buf, &n) != ESP_OK || n == 0) {
            break;
        }
        xStreamBufferSend(rx_sb, buf, n, 0);
    } while (n == sizeof buf);
    uint64_t one = 1;
    write(evfd, &one, sizeof one);
}

int usb_link_init(void)
{
    esp_vfs_eventfd_config_t ecfg = ESP_VFS_EVENTD_CONFIG_DEFAULT();
    if (esp_vfs_eventfd_register(&ecfg) != ESP_OK) {
        return -1;
    }
    evfd = eventfd(0, 0);
    if (evfd >= 0) {
        fcntl(evfd, F_SETFL, O_NONBLOCK);
    }
    rx_sb = xStreamBufferCreate(16384, 1);
    if (evfd < 0 || !rx_sb) {
        return -1;
    }
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(serial, sizeof serial, "%02X%02X%02X%02X%02X%02X", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    static const char *strings[] = {
        lang_id,                      /* 0: language ID */
        "tether",                     /* 1: manufacturer */
        "tether-node",                /* 2: product: the host recognises the node by this string */
        serial,                       /* 3: serial number */
        "tether link",                /* 4: CDC interface */
    };
    const tinyusb_config_t tusb = {
        .device_descriptor = NULL,    /* default Espressif VID/PID from Kconfig */
        .string_descriptor = strings,
        .string_descriptor_count = sizeof strings / sizeof strings[0],
        .external_phy = false,
        .configuration_descriptor = NULL,
    };
    if (tinyusb_driver_install(&tusb) != ESP_OK) {
        return -1;
    }
    const tinyusb_config_cdcacm_t acm = {
        .usb_dev = TINYUSB_USBDEV_0,
        .cdc_port = TINYUSB_CDC_ACM_0,
        .rx_unread_buf_sz = 2048,
        .callback_rx = rx_cb,
        .callback_rx_wanted_char = NULL,
        .callback_line_state_changed = NULL,
        .callback_line_coding_changed = NULL,
    };
    if (tusb_cdc_acm_init(&acm) != ESP_OK) {
        return -1;
    }
    ESP_LOGI(TAG, "CDC-ACM up, serial %s", serial);
    return 0;
}

int usb_link_fd(void) { return evfd; }

int usb_link_read(uint8_t *buf, size_t cap)
{
    uint64_t v;
    read(evfd, &v, sizeof v);   /* clear the wake-up first: data that arrives after this re-signals it */
    size_t n = xStreamBufferReceive(rx_sb, buf, cap, 0);
    if (n > 0 && xStreamBufferBytesAvailable(rx_sb) > 0) {
        uint64_t one = 1;
        write(evfd, &one, sizeof one);   /* more is waiting; make sure the next select() wakes up */
    }
    return (int)n;
}

int usb_link_write(const uint8_t *buf, size_t n, int may_drop)
{
    if (!tud_cdc_n_connected(0)) {
        return may_drop ? 0 : -1;
    }
    size_t off = 0;
    int64_t t0 = esp_timer_get_time();
    while (off < n) {
        size_t q = tinyusb_cdcacm_write_queue(TINYUSB_CDC_ACM_0, buf + off, n - off);
        off += q;
        if (off < n) {
            if (may_drop) {
                return 0;
            }
            tinyusb_cdcacm_write_flush(TINYUSB_CDC_ACM_0, pdMS_TO_TICKS(100));
            if (q == 0 && esp_timer_get_time() - t0 > 5000000) {
                return -1;   /* the host has not read for 5 s */
            }
        }
    }
    tinyusb_cdcacm_write_flush(TINYUSB_CDC_ACM_0, 0);
    return 0;
}
