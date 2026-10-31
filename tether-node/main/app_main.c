#include "esp_system.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "platform_esp.h"
#include "tc_engine.h"
#include "usb_link.h"
#include "wifi_mgr.h"

static const char *TAG = "main";

static void net_task(void *arg)
{
    tc_platform p;
    platform_esp_fill(&p);
    tc_engine *e = tc_engine_new(&p);
    if (!e) {
        ESP_LOGE(TAG, "engine allocation failed");
        vTaskDelete(NULL);
    }
    while (tc_engine_poll(e, 100) >= 0) {
    }
    ESP_LOGE(TAG, "engine stopped");
    esp_restart();
}

void app_main(void)
{
    esp_err_t r = nvs_flash_init();   /* needed by the Wi-Fi driver only; nothing of ours is stored */
    if (r == ESP_ERR_NVS_NO_FREE_PAGES || r == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
    if (usb_link_init() != 0) {
        ESP_LOGE(TAG, "USB init failed");
        esp_restart();
    }
    wifi_mgr_init();
    xTaskCreate(net_task, "net", 8192, NULL, 5, NULL);
}
