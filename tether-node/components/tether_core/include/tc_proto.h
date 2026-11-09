/* Wire protocol: framing and payload layout. Normative text: ARCHITECTURE.md section 4. */
#ifndef TC_PROTO_H
#define TC_PROTO_H
#include <stddef.h>
#include <stdint.h>

#define TC_PROTO_VERSION 1
#define TC_MAX_PAYLOAD 1024
#define TC_MAX_BODY (4 + TC_MAX_PAYLOAD + 4)
#define TC_MAX_RAW (TC_MAX_BODY + TC_MAX_BODY / 254 + 2)
#define TC_MAX_CHANNELS 16
#define TC_MAX_LISTENERS 4
#define TC_MAX_CONFIG 8192
#define TC_MAX_FORWARDS 8
#define TC_HOST_CHAN_BASE 0x8000   /* ids chosen by the host (OPEN) are TC_HOST_CHAN_BASE + 1..TC_MAX_CHANNELS */
#define TC_FLAG_MORE 0x01

enum {
    TC_T_HELLO = 0x01, TC_T_PING = 0x02, TC_T_PONG = 0x03,
    TC_T_CONFIG = 0x10, TC_T_STATUS = 0x11, TC_T_CONFIG_RES = 0x12, TC_T_LOG = 0x13, TC_T_REBOOT = 0x14,
    TC_T_ACCEPT = 0x20, TC_T_OPEN = 0x21, TC_T_OPEN_RES = 0x22, TC_T_DATA = 0x30, TC_T_CREDIT = 0x31, TC_T_CLOSE = 0x32
};
enum { TC_CLOSE_EOF = 0, TC_CLOSE_RESET = 1, TC_CLOSE_PROTO = 2, TC_CLOSE_NO_CHANNEL = 3 };
enum { TC_OPEN_OK = 0, TC_OPEN_REFUSED = 1, TC_OPEN_UNREACHABLE = 2, TC_OPEN_DNS = 3, TC_OPEN_NO_CAPACITY = 4,
       TC_OPEN_UNKNOWN_FORWARD = 5, TC_OPEN_BAD_REQUEST = 6, TC_OPEN_NOT_READY = 7 };
enum { TC_ROLE_HOST = 0, TC_ROLE_NODE = 1 };
enum { TC_STATE_NEEDS_CONFIG = 0, TC_STATE_RUNNING = 1 };

typedef struct {
    uint8_t type, flags;
    uint16_t chan;
    size_t plen;
    uint8_t payload[TC_MAX_PAYLOAD];
} tc_frame;

typedef struct {
    uint8_t buf[TC_MAX_RAW];
    uint8_t body[TC_MAX_BODY];
    size_t len;
    int overflow;
    uint32_t errors;
} tc_deframer;

uint32_t tc_crc32(uint32_t crc, const uint8_t *buf, size_t len);   /* zlib.crc32 compatible */
size_t tc_cobs_encode(const uint8_t *in, size_t n, uint8_t *out);  /* out needs n + n/254 + 2 bytes */
int tc_cobs_decode(const uint8_t *in, size_t n, uint8_t *out);     /* decoded length, or -1 */
/* Encode one frame including the trailing 0x00. Returns the length, or 0 if it does not fit. */
size_t tc_frame_encode(uint8_t type, uint8_t flags, uint16_t chan, const uint8_t *payload, size_t plen,
                       uint8_t *out, size_t cap);
void tc_deframer_init(tc_deframer *d);
/* Feed one byte. Returns 1 when `f` holds a complete valid frame. Errors are counted in d->errors. */
int tc_deframer_push(tc_deframer *d, uint8_t b, tc_frame *f);

static inline void tc_put16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static inline void tc_put32(uint8_t *p, uint32_t v) { tc_put16(p, (uint16_t)v); tc_put16(p + 2, (uint16_t)(v >> 16)); }
static inline uint16_t tc_get16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }
static inline uint32_t tc_get32(const uint8_t *p) { return tc_get16(p) | ((uint32_t)tc_get16(p + 2) << 16); }

#endif
