#include "platform_esp.h"

#include <fcntl.h>
#include <unistd.h>

#include "esp_log.h"
#include "esp_random.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_vfs_eventfd.h"
#include "soc/rtc_cntl_reg.h"
#include "usb_link.h"
#include "wifi_mgr.h"

static const char *TAG = "node";
static int wake_fd = -1;

static void p_wake(void *c)   /* called from the DNS worker threads */
{
    uint64_t one = 1;
    write(wake_fd, &one, sizeof one);
}

static int p_read(void *c, uint8_t *b, size_t n) { return usb_link_read(b, n); }
static int p_write(void *c, const uint8_t *b, size_t n, int drop) { return usb_link_write(b, n, drop); }
static uint32_t p_millis(void *c) { return (uint32_t)(esp_timer_get_time() / 1000); }
static uint32_t p_random(void *c) { return esp_random(); }
static int p_wifi(void *c, const tc_wifi_cfg *w, char *e, size_t n) { return wifi_mgr_apply(w, e, n); }
static void p_lst(void *c, const tc_config *cfg) { wifi_mgr_listeners(cfg); }
static int p_subnet(void *c, uint32_t l, uint32_t *n, uint32_t *m) { return wifi_mgr_subnet(l, n, m); }
static void p_status(void *c, tc_status *s) { wifi_mgr_status(s); }

static void p_reboot(void *c, int mode)
{
    ESP_LOGW(TAG, "reboot requested, mode %d", mode);
    if (mode == 1) {
        REG_WRITE(RTC_CNTL_OPTION1_REG, RTC_CNTL_FORCE_DOWNLOAD_BOOT);   /* ROM download mode after reset (verify on hardware) */
    }
    esp_restart();
}

static void p_log(void *c, int level, const char *m)
{
    switch (level) {
    case 0: ESP_LOGE(TAG, "%s", m); break;
    case 1: ESP_LOGW(TAG, "%s", m); break;
    case 2: ESP_LOGI(TAG, "%s", m); break;
    default: ESP_LOGD(TAG, "%s", m); break;
    }
}

void platform_esp_fill(tc_platform *p)
{
    wake_fd = eventfd(0, 0);
    if (wake_fd >= 0) {
        fcntl(wake_fd, F_SETFL, O_NONBLOCK);
    }
    *p = (tc_platform){
        .version = "tether-node 0.1",
        .link_fd = usb_link_fd(),
        .wake_fd = wake_fd,
        .wake = p_wake,
        .link_read = p_read,
        .link_write = p_write,
        .millis = p_millis,
        .random32 = p_random,
        .apply_wifi = p_wifi,
        .apply_listeners = p_lst,
        .iface_subnet = p_subnet,
        .get_status = p_status,
        .reboot = p_reboot,
        .log = p_log,
    };
}
