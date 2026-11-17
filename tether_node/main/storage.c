#include "storage.h"

#include "esp_log.h"
#include "esp_partition.h"
#include "tinyusb_msc.h"
#include "wear_levelling.h"

static const char *TAG = "storage";
static tinyusb_msc_storage_handle_t hdl;

static void msc_event(tinyusb_msc_storage_handle_t h, tinyusb_msc_event_t *ev, void *arg)
{
    const char *who = ev->mount_point == TINYUSB_MSC_STORAGE_MOUNT_USB ? "the PC (USB drive)" : "the node (" STORAGE_PATH ")";
    switch (ev->id) {
    case TINYUSB_MSC_EVENT_MOUNT_COMPLETE:
        ESP_LOGI(TAG, "drive now belongs to %s", who);
        break;
    case TINYUSB_MSC_EVENT_MOUNT_FAILED:
        ESP_LOGW(TAG, "handing the drive to %s failed", who);
        break;
    case TINYUSB_MSC_EVENT_FORMAT_REQUIRED:
        ESP_LOGI(TAG, "no FAT file system yet: formatting");
        break;
    case TINYUSB_MSC_EVENT_FORMAT_FAILED:
        ESP_LOGE(TAG, "formatting the drive failed");
        break;
    default:
        break;
    }
}

int storage_init(void)
{
    const esp_partition_t *part = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_DATA_FAT, "storage");
    if (!part) {
        ESP_LOGE(TAG, "no 'storage' partition in the partition table: USB drive disabled");
        return -1;
    }
    wl_handle_t wl = WL_INVALID_HANDLE;
    esp_err_t r = wl_mount(part, &wl);
    if (r != ESP_OK) {
        ESP_LOGE(TAG, "wear levelling mount failed: %s", esp_err_to_name(r));
        return -1;
    }
    const tinyusb_msc_storage_config_t cfg = {
        .medium.wl_handle = wl,
        .fat_fs = {
            .base_path = (char *)STORAGE_PATH,
            .config = {.max_files = 4, .format_if_mount_failed = true},
            .do_not_format = false,
            .format_flags = 0,
        },
        .mount_point = TINYUSB_MSC_STORAGE_MOUNT_APP,   /* the node's until the PC enumerates the device */
    };
    r = tinyusb_msc_new_storage_spiflash(&cfg, &hdl);
    if (r != ESP_OK) {
        ESP_LOGE(TAG, "tinyusb_msc_new_storage_spiflash: %s", esp_err_to_name(r));
        hdl = NULL;
        return -1;
    }
    tinyusb_msc_set_storage_callback(msc_event, NULL);
    uint32_t sectors = 0, size = 0;
    tinyusb_msc_get_storage_capacity(hdl, &sectors);
    tinyusb_msc_get_storage_sector_size(hdl, &size);
    ESP_LOGI(TAG, "%u KB FAT volume (%u sectors of %u bytes) in partition 'storage'", (unsigned)(sectors / 1024 * size),
             (unsigned)sectors, (unsigned)size);
    return 0;
}

const char *storage_owner(void)
{
    tinyusb_msc_mount_point_t mp;
    if (!hdl || tinyusb_msc_get_storage_mount_point(hdl, &mp) != ESP_OK) {
        return "off";
    }
    return mp == TINYUSB_MSC_STORAGE_MOUNT_USB ? "pc" : "node";
}
