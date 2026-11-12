idf_component_register(SRCS "app_main.c" "usb_link.c" "wifi_mgr.c" "platform_esp.c" "storage.c"
                       INCLUDE_DIRS "."
                       REQUIRES tether_core
                       PRIV_REQUIRES esp_wifi esp_netif esp_event nvs_flash esp_timer esp_system esp_hw_support
                                     freertos vfs lwip mdns fatfs wear_levelling esp_partition espressif__esp_tinyusb)
