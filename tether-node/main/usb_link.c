/* CDC-ACM link to tether-host over the native USB port (esp_tinyusb 2.x), plus the USB drive (storage.c).
 *
 * Received bytes stay in TinyUSB's own FIFO until the net task reads them: when the net task falls behind, the FIFO fills
 * and USB itself makes the PC wait (no bytes are ever dropped). The receive callback only wakes the net task. */
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
#include "tinyusb.h"
#include "tinyusb_cdc_acm.h"
#include "tinyusb_default_config.h"
#include "storage.h"

static const char *TAG = "usb";
static int evfd = -1;
static char serial[20];
static const char lang_id[] = {0x09, 0x04};   /* language ID: English (US) */
static uint32_t stalls;

static void signal_rx(void)
{
    uint64_t one = 1;
    write(evfd, &one, sizeof one);
}

static void rx_cb(int itf, cdcacm_event_t *event)
{
    signal_rx();
}

static void line_state_cb(int itf, cdcacm_event_t *event)
{
    /* DTR is set while a program has the COM port open (pyserial sets it by default) */
    ESP_LOGI(TAG, "host %s the port (DTR=%d RTS=%d)", event->line_state_changed_data.dtr ? "opened" : "closed",
             event->line_state_changed_data.dtr, event->line_state_changed_data.rts);
}

int usb_link_init(void)
{
    esp_vfs_eventfd_config_t ecfg = ESP_VFS_EVENTD_CONFIG_DEFAULT();
    esp_err_t r = esp_vfs_eventfd_register(&ecfg);
    if (r != ESP_OK) {
        ESP_LOGE(TAG, "eventfd registration failed: %s", esp_err_to_name(r));
        return -1;
    }
    evfd = eventfd(0, 0);   /* never blocks on read in ESP-IDF: an empty eventfd reads as 0 */
    if (evfd < 0) {
        ESP_LOGE(TAG, "eventfd() failed");
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
        "tether storage",             /* 5: MSC interface (the USB drive) */
    };
    /* device and configuration descriptors: defaults from Kconfig (VID:PID 1209:0001, CDC + MSC, see sdkconfig.defaults) */
    tinyusb_config_t tusb = TINYUSB_DEFAULT_CONFIG();
    tusb.descriptor.string = strings;
    tusb.descriptor.string_count = sizeof strings / sizeof strings[0];
    storage_init();   /* before the driver: the drive's storage must exist when the MSC class starts */
    r = tinyusb_driver_install(&tusb);
    if (r != ESP_OK) {
        ESP_LOGE(TAG, "tinyusb_driver_install failed: %s", esp_err_to_name(r));
        return -1;
    }
    const tinyusb_config_cdcacm_t acm = {
        .cdc_port = TINYUSB_CDC_ACM_0,
        .callback_rx = rx_cb,
        .callback_rx_wanted_char = NULL,
        .callback_line_state_changed = line_state_cb,
        .callback_line_coding_changed = NULL,
    };
    r = tinyusb_cdcacm_init(&acm);
    if (r != ESP_OK) {
        ESP_LOGE(TAG, "tinyusb_cdcacm_init failed: %s", esp_err_to_name(r));
        return -1;
    }
    ESP_LOGI(TAG, "CDC-ACM up, product tether-node, serial %s", serial);
    return 0;
}

int usb_link_fd(void) { return evfd; }

int usb_link_read(uint8_t *buf, size_t cap)
{
    uint64_t v;
    read(evfd, &v, sizeof v);   /* clear the wake-up first: data that arrives after this re-signals it */
    size_t n = 0;
    if (tinyusb_cdcacm_read(TINYUSB_CDC_ACM_0, buf, cap, &n) != ESP_OK) {
        return 0;
    }
    if (n > 0 && tud_cdc_n_available(0) > 0) {
        signal_rx();   /* more is waiting; make sure the next select() wakes up */
    }
    return (int)n;
}

int usb_link_write(const uint8_t *buf, size_t n, int may_drop)
{
    if (!tud_cdc_n_connected(0)) {
        ESP_LOGD(TAG, "no host on the port (DTR low): %s %u bytes", may_drop ? "dropping" : "cannot send", (unsigned)n);
        return may_drop ? 0 : -1;
    }
    size_t off = 0;
    int64_t t0 = esp_timer_get_time();
    while (off < n) {
        size_t q = tinyusb_cdcacm_write_queue(TINYUSB_CDC_ACM_0, buf + off, n - off);
        off += q;
        if (off < n) {
            if (may_drop && off == 0) {
                return 0;   /* nothing of this frame was queued, so dropping it keeps the stream intact */
            }
            tinyusb_cdcacm_write_flush(TINYUSB_CDC_ACM_0, pdMS_TO_TICKS(100));
            if (q == 0 && esp_timer_get_time() - t0 > 5000000) {
                ESP_LOGW(TAG, "host has not read for 5 s (%u stalls so far); %u of %u bytes sent", (unsigned)++stalls,
                         (unsigned)off, (unsigned)n);
                return -1;
            }
        }
    }
    tinyusb_cdcacm_write_flush(TINYUSB_CDC_ACM_0, 0);
    return 0;
}
