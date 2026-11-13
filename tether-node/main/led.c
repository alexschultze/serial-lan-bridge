/* Status LED on the on-board WS2812. Read-only observer of USB, Wi-Fi and link activity; never blocks the net task. */
#include "led.h"

#include "sdkconfig.h"

#if CONFIG_TETHER_LED_ENABLE

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "led_strip.h"
#include "tinyusb.h"
#include "wifi_mgr.h"

static const char *TAG = "led";
static led_strip_handle_t strip;
static volatile int64_t last_activity_us;

void led_activity(void) { last_activity_us = esp_timer_get_time(); }

static void show(uint32_t r, uint32_t g, uint32_t b, uint32_t pct)
{
    uint32_t k = CONFIG_TETHER_LED_BRIGHTNESS * pct / 100;   /* pct: 0..100 */
    led_strip_set_pixel(strip, 0, r * k / 255, g * k / 255, b * k / 255);
    led_strip_refresh(strip);
}

static void led_task(void *arg)
{
    for (;;) {
        int64_t now = esp_timer_get_time();
        int wifi = wifi_mgr_led_state();   /* bit 0: station has an IP, bit 1: hotspot on */
        if (now - last_activity_us < 60000) {
            show(255, 255, 255, 100);                       /* traffic: white flash */
        } else if (!tud_cdc_n_connected(0)) {
            show(255, 0, 0, 100);                           /* no host program on the COM port: red */
        } else if (wifi & 1) {
            show(0, 255, 0, 100);                           /* Wi-Fi joined: green */
        } else if (wifi & 2) {
            show(0, 255, 255, 100);                         /* hotspot only: cyan */
        } else {
            int phase = (int)((now / 1000) % 1200);         /* 1.2 s triangle pulse: blue while joining */
            int tri = phase < 600 ? phase : 1200 - phase;
            show(0, 0, 255, 15 + tri * 85 / 600);
        }
        vTaskDelay(pdMS_TO_TICKS(30));
    }
}

void led_init(void)
{
    led_strip_config_t sc = {
        .strip_gpio_num = CONFIG_TETHER_LED_GPIO,
        .max_leds = 1,
        .led_model = LED_MODEL_WS2812,
        .led_pixel_format = LED_PIXEL_FORMAT_GRB,
    };
    led_strip_rmt_config_t rc = {.clk_src = RMT_CLK_SRC_DEFAULT, .resolution_hz = 10 * 1000 * 1000};
    if (led_strip_new_rmt_device(&sc, &rc, &strip) != ESP_OK) {
        ESP_LOGW(TAG, "LED on GPIO%d not available", CONFIG_TETHER_LED_GPIO);
        return;
    }
    show(0, 0, 255, 100);   /* booting: blue */
    xTaskCreate(led_task, "led", 3072, NULL, 1, NULL);
}

#else

void led_init(void) {}
void led_activity(void) {}

#endif
