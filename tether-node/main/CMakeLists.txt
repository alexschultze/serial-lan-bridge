idf_component_register(SRCS "app_main.c" "usb_link.c" "wifi_mgr.c" "platform_esp.c" "storage.c"
                       INCLUDE_DIRS "."
                       REQUIRES tether_core
                       PRIV_REQUIRES esp_wifi esp_netif esp_event nvs_flash esp_timer esp_system esp_hw_support
                                     freertos vfs lwip mdns fatfs wear_levelling esp_partition espressif__esp_tinyusb)

# Firmware version: TETHER_VERSION from the environment (the release script sets it from the git tag), else "dev".
# It is read when CMake configures the project: after changing it, build from clean.
if(DEFINED ENV{TETHER_VERSION})
    set(TETHER_VERSION "$ENV{TETHER_VERSION}")
else()
    set(TETHER_VERSION "dev")
endif()
target_compile_definitions(${COMPONENT_LIB} PRIVATE TETHER_VERSION="${TETHER_VERSION}")
