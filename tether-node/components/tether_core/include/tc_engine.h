/* Portable node engine: owns listeners, channels, flow control and the control protocol.
 * It uses BSD sockets (lwIP on the ESP32, the OS on Linux) and talks to the hardware through tc_platform. */
#ifndef TC_ENGINE_H
#define TC_ENGINE_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "tc_proto.h"

#define TC_MAX_NETWORKS 4
#define TC_MAX_ALLOW 8

typedef struct { uint32_t net, mask; } tc_cidr;                 /* host byte order */
typedef struct { char ssid[33]; char psk[64]; bool hidden; } tc_net;

/* Everything the Wi-Fi manager needs; compared with memcmp to detect changes, so always built zero-initialised. */
typedef struct {
    char country[3];
    uint8_t n_nets;
    tc_net nets[TC_MAX_NETWORKS];
    bool hotspot;                /* section present and mode != never */
    uint8_t hs_mode;             /* 0 auto, 1 always */
    char hs_ssid[33];            /* empty: the platform derives tether-XXXX */
    char hs_psk[64];
    uint16_t hs_fallback;        /* seconds */
} tc_wifi_cfg;

typedef struct {
    uint16_t id, port;
    bool http;
    uint8_t n_allow;
    tc_cidr allow[TC_MAX_ALLOW];
} tc_listener_cfg;

typedef struct {
    uint16_t id, port;
    char host[64];               /* IPv4 literal or a name resolved on the node's network */
} tc_forward_cfg;

typedef struct {
    tc_wifi_cfg wifi;
    uint8_t n_lst;
    tc_listener_cfg lst[TC_MAX_LISTENERS];
    uint8_t n_fwd;
    tc_forward_cfg fwd[TC_MAX_FORWARDS];
    uint16_t window;
    uint32_t never_used_s, idle_s;
} tc_config;

/* Parse and validate a CONFIG JSON blob. Returns 0 on success; otherwise a NUL-terminated list of problems in err. */
int tc_config_parse(const char *json, size_t len, tc_config *out, char *err, size_t errcap);

typedef struct {
    const char *mode;        /* off | sta | ap | ap+sta */
    char ssid[33];
    char ip[16];
    char hostname[40];
    int rssi;
    int ap_clients;
    uint32_t heap_free;
    char note[64];           /* last Wi-Fi problem in plain words ("wrong password?"), empty when fine */
} tc_status;

typedef struct {
    void *ctx;
    const char *version;
    int wake_fd;                                                        /* readable after wake() was called (resolver threads); or -1 */
    void (*wake)(void *ctx);                                            /* thread-safe: make the engine's select() return */
    int link_fd;                                                        /* select()able fd that is readable when link_read has data, or -1 */
    int (*link_read)(void *ctx, uint8_t *buf, size_t cap);              /* >0 bytes, 0 none, <0 fatal */
    int (*link_write)(void *ctx, const uint8_t *buf, size_t n, int may_drop);  /* 0 ok (or dropped when may_drop), <0 stalled/closed */
    uint32_t (*millis)(void *ctx);
    uint32_t (*random32)(void *ctx);
    int (*apply_wifi)(void *ctx, const tc_wifi_cfg *cfg, char *err, size_t errcap);   /* 0 ok */
    void (*apply_listeners)(void *ctx, const tc_config *cfg);          /* optional: e.g. mDNS service records */
    int (*iface_subnet)(void *ctx, uint32_t local_ip, uint32_t *net, uint32_t *mask); /* host byte order; 0 if known */
    void (*get_status)(void *ctx, tc_status *st);
    void (*reboot)(void *ctx, int mode);
    void (*log)(void *ctx, int level, const char *msg);
} tc_platform;

typedef struct tc_engine tc_engine;
tc_engine *tc_engine_new(const tc_platform *plat);
void tc_engine_free(tc_engine *e);
/* One iteration of the event loop, waits at most timeout_ms. Returns 0, or <0 if the link is gone for good. */
int tc_engine_poll(tc_engine *e, int timeout_ms);
int tc_engine_state(const tc_engine *e);             /* TC_STATE_* */
int tc_engine_listener_port(const tc_engine *e, int listener_id);   /* bound port, for tests (port 0 requests) */

#endif
