/* Wi-Fi station with DHCP, fallback hotspot, mDNS. State machine per ARCHITECTURE.md section 7. */
#include "wifi_mgr.h"

#include <stdio.h>
#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lwip/ip4_addr.h"
#include "mdns.h"

static const char *TAG = "wifi";

static esp_netif_t *sta_if, *ap_if;
static SemaphoreHandle_t lock;
static tc_wifi_cfg cfg;
static bool have_cfg, cfg_new;
static volatile bool sta_up;          /* station has an IP */
static bool ap_on;
static volatile int ap_clients;
static int64_t t_lost_ms;             /* when the station last had no connection */
static int64_t next_try_ms;
static int backoff_s = 1;
static int ap_subnet = 77;            /* 10.42.<ap_subnet>.0/24 */
static char hostname[24];
static char cur_ssid[33];

static int64_t now_ms(void) { return esp_timer_get_time() / 1000; }

static void ap_ssid(char *out, size_t cap)
{
    if (cfg.hs_ssid[0]) {
        snprintf(out, cap, "%s", cfg.hs_ssid);
    } else {
        snprintf(out, cap, "%s", hostname);
    }
}

static void ap_set_ip(void)
{
    esp_netif_ip_info_t ip;
    IP4_ADDR(&ip.ip, 10, 42, ap_subnet, 1);
    IP4_ADDR(&ip.gw, 10, 42, ap_subnet, 1);
    IP4_ADDR(&ip.netmask, 255, 255, 255, 0);
    esp_netif_dhcps_stop(ap_if);
    esp_netif_set_ip_info(ap_if, &ip);
    esp_netif_dhcps_start(ap_if);
}

static void ap_start(void)
{
    wifi_config_t ac = {0};
    char ssid[33];
    ap_ssid(ssid, sizeof ssid);
    strlcpy((char *)ac.ap.ssid, ssid, sizeof ac.ap.ssid);
    ac.ap.ssid_len = strlen(ssid);
    strlcpy((char *)ac.ap.password, cfg.hs_psk, sizeof ac.ap.password);
    ac.ap.authmode = WIFI_AUTH_WPA2_PSK;   /* never open */
    ac.ap.max_connection = 4;
    ac.ap.channel = 1;
    esp_wifi_set_mode(WIFI_MODE_APSTA);
    esp_wifi_set_config(WIFI_IF_AP, &ac);
    ap_set_ip();
    ap_on = true;
    ESP_LOGI(TAG, "hotspot '%s' up on 10.42.%d.1", ssid, ap_subnet);
}

static void ap_stop(void)
{
    esp_wifi_set_mode(WIFI_MODE_STA);
    ap_on = false;
    ap_clients = 0;
    ESP_LOGI(TAG, "hotspot stopped");
}

static void try_connect(void)
{
    wifi_scan_config_t sc = {.show_hidden = true};
    if (esp_wifi_scan_start(&sc, true) != ESP_OK) {
        return;
    }
    uint16_t n = 20;
    wifi_ap_record_t recs[20];
    esp_wifi_scan_get_ap_records(&n, recs);
    int best = -1;
    int8_t best_rssi = -127;
    for (int i = 0; i < cfg.n_nets; i++) {
        for (int k = 0; k < n; k++) {
            if (strcmp((char *)recs[k].ssid, cfg.nets[i].ssid) == 0 && recs[k].rssi > best_rssi) {
                best = i;
                best_rssi = recs[k].rssi;
            }
        }
    }
    if (best < 0) {
        for (int i = 0; i < cfg.n_nets; i++) {
            if (cfg.nets[i].hidden) { best = i; break; }
        }
    }
    if (best < 0) {
        return;
    }
    wifi_config_t wc = {0};
    strlcpy((char *)wc.sta.ssid, cfg.nets[best].ssid, sizeof wc.sta.ssid);
    strlcpy((char *)wc.sta.password, cfg.nets[best].psk, sizeof wc.sta.password);
    wc.sta.threshold.authmode = cfg.nets[best].psk[0] ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
    wc.sta.scan_method = WIFI_ALL_CHANNEL_SCAN;
    esp_wifi_set_config(WIFI_IF_STA, &wc);
    strlcpy(cur_ssid, cfg.nets[best].ssid, sizeof cur_ssid);
    ESP_LOGI(TAG, "joining '%s'", cur_ssid);
    esp_wifi_connect();
}

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT) {
        if (id == WIFI_EVENT_STA_DISCONNECTED) {
            if (sta_up) {
                ESP_LOGW(TAG, "station lost");
            }
            sta_up = false;
            t_lost_ms = now_ms();
        } else if (id == WIFI_EVENT_AP_STACONNECTED) {
            ap_clients++;
        } else if (id == WIFI_EVENT_AP_STADISCONNECTED && ap_clients > 0) {
            ap_clients--;
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *ev = data;
        sta_up = true;
        backoff_s = 1;
        ESP_LOGI(TAG, "got " IPSTR, IP2STR(&ev->ip_info.ip));
        if (ap_on && (ev->ip_info.ip.addr & 0xFFFFFF) == (PP_HTONL(0x0A2A0000u | ((uint32_t)ap_subnet << 8)) & 0xFFFFFF)) {
            ap_subnet = ap_subnet == 77 ? 78 : 77;   /* the station's network uses our hotspot range */
            ap_set_ip();
        }
    }
}

static void mgr_task(void *arg)
{
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(500));
        xSemaphoreTake(lock, portMAX_DELAY);
        if (!have_cfg) {
            xSemaphoreGive(lock);
            continue;
        }
        int64_t now = now_ms();
        if (cfg_new) {
            cfg_new = false;
            esp_wifi_set_country_code(cfg.country, true);
            if (cfg.hotspot && cfg.hs_mode == 1) {
                if (!ap_on) ap_start();
            } else if (ap_on && sta_up) {
                ap_stop();
            }
            t_lost_ms = now;
            next_try_ms = 0;
            backoff_s = 1;
            esp_wifi_disconnect();
        }
        if (!sta_up) {
            bool postpone = ap_on && ap_clients > 0;   /* a scan briefly interrupts the hotspot */
            if (now >= next_try_ms && !postpone) {
                try_connect();
                next_try_ms = now + (ap_on ? 60000 : backoff_s * 1000);
                if (backoff_s < 60) backoff_s *= 2;
            }
            if (cfg.hotspot && !ap_on && now - t_lost_ms >= cfg.hs_fallback * 1000LL) {
                ap_start();
            }
        } else if (ap_on && cfg.hs_mode == 0 && ap_clients == 0) {
            ap_stop();
        }
        xSemaphoreGive(lock);
    }
}

void wifi_mgr_init(void)
{
    lock = xSemaphoreCreateMutex();
    esp_netif_init();
    esp_event_loop_create_default();
    sta_if = esp_netif_create_default_wifi_sta();
    ap_if = esp_netif_create_default_wifi_ap();
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(hostname, sizeof hostname, "tether-%02x%02x", mac[4], mac[5]);
    esp_netif_set_hostname(sta_if, hostname);
    wifi_init_config_t wic = WIFI_INIT_CONFIG_DEFAULT();
    esp_wifi_init(&wic);
    esp_wifi_set_storage(WIFI_STORAGE_RAM);
    esp_wifi_set_mode(WIFI_MODE_STA);
    esp_wifi_set_ps(WIFI_PS_NONE);   /* latency for streaming */
    esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL);
    esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL);
    esp_wifi_start();
    mdns_init();
    mdns_hostname_set(hostname);
    mdns_instance_name_set("tether-node");
    xTaskCreate(mgr_task, "wifi_mgr", 4096, NULL, 4, NULL);
}

int wifi_mgr_apply(const tc_wifi_cfg *c, char *err, size_t errcap)
{
    if (strlen(c->country) != 2) {
        snprintf(err, errcap, "bad country");
        return -1;
    }
    xSemaphoreTake(lock, portMAX_DELAY);
    memcpy(&cfg, c, sizeof cfg);
    have_cfg = true;
    cfg_new = true;
    xSemaphoreGive(lock);
    return 0;
}

void wifi_mgr_listeners(const tc_config *c)
{
    mdns_service_remove_all();
    for (int i = 0; i < c->n_lst; i++) {
        char port[8];
        snprintf(port, sizeof port, "%u", c->lst[i].port);
        mdns_txt_item_t txt[1] = {{"id", port}};
        mdns_service_add(NULL, "_tether", "_tcp", c->lst[i].port, txt, 1);
        break;   /* one record is enough for discovery; the port is in the TXT */
    }
}

void wifi_mgr_status(tc_status *st)
{
    esp_netif_ip_info_t ip;
    wifi_ap_record_t ap;
    st->mode = sta_up ? (ap_on ? "ap+sta" : "sta") : (ap_on ? "ap" : "off");
    snprintf(st->hostname, sizeof st->hostname, "%s.local", hostname);
    st->ap_clients = ap_clients;
    st->heap_free = esp_get_free_heap_size();
    if (sta_up && esp_netif_get_ip_info(sta_if, &ip) == ESP_OK) {
        snprintf(st->ip, sizeof st->ip, IPSTR, IP2STR(&ip.ip));
        if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
            st->rssi = ap.rssi;
            strlcpy(st->ssid, (char *)ap.ssid, sizeof st->ssid);
        }
    } else if (ap_on && esp_netif_get_ip_info(ap_if, &ip) == ESP_OK) {
        snprintf(st->ip, sizeof st->ip, IPSTR, IP2STR(&ip.ip));
        ap_ssid(st->ssid, sizeof st->ssid);
    }
}

int wifi_mgr_subnet(uint32_t local, uint32_t *net, uint32_t *mask)
{
    esp_netif_t *ifs[2] = {sta_if, ap_if};
    for (int i = 0; i < 2; i++) {
        esp_netif_ip_info_t ip;
        if (esp_netif_get_ip_info(ifs[i], &ip) == ESP_OK && ip.ip.addr && lwip_ntohl(ip.ip.addr) == local) {
            *mask = lwip_ntohl(ip.netmask.addr);
            *net = local & *mask;
            return 0;
        }
    }
    return -1;
}
