#include "tc_proto.h"

#include <string.h>

uint32_t tc_crc32(uint32_t crc, const uint8_t *buf, size_t len)
{
    static const uint32_t nib[16] = {0x00000000, 0x1db71064, 0x3b6e20c8, 0x26d930ac, 0x76dc4190, 0x6b6b51f4,
                                     0x4db26158, 0x5005713c, 0xedb88320, 0xf00f9344, 0xd6d6a3e8, 0xcb61b38c,
                                     0x9b64c2b0, 0x86d3d2d4, 0xa00ae278, 0xbdbdf21c};
    crc = ~crc;
    for (size_t i = 0; i < len; i++) {
        crc ^= buf[i];
        crc = (crc >> 4) ^ nib[crc & 0x0f];
        crc = (crc >> 4) ^ nib[crc & 0x0f];
    }
    return ~crc;
}

size_t tc_cobs_encode(const uint8_t *in, size_t n, uint8_t *out)
{
    size_t o = 1, code_pos = 0;
    uint8_t code = 1;
    for (size_t i = 0; i < n; i++) {
        if (in[i] == 0) {
            out[code_pos] = code;
            code_pos = o++;
            code = 1;
        } else {
            out[o++] = in[i];
            if (++code == 0xFF) {
                out[code_pos] = code;
                code_pos = o++;
                code = 1;
            }
        }
    }
    out[code_pos] = code;
    return o;
}

int tc_cobs_decode(const uint8_t *in, size_t n, uint8_t *out)
{
    size_t i = 0, o = 0;
    while (i < n) {
        uint8_t code = in[i];
        if (code == 0 || i + code > n) {
            return -1;
        }
        memcpy(out + o, in + i + 1, (size_t)code - 1);
        o += (size_t)code - 1;
        i += code;
        if (code != 0xFF && i < n) {
            out[o++] = 0;
        }
    }
    return (int)o;
}

size_t tc_frame_encode(uint8_t type, uint8_t flags, uint16_t chan, const uint8_t *payload, size_t plen,
                       uint8_t *out, size_t cap)
{
    if (plen > TC_MAX_PAYLOAD) {
        return 0;
    }
    uint8_t body[TC_MAX_BODY];
    body[0] = type;
    body[1] = flags;
    tc_put16(body + 2, chan);
    if (plen) {
        memcpy(body + 4, payload, plen);
    }
    tc_put32(body + 4 + plen, tc_crc32(0, body, 4 + plen));
    size_t blen = 8 + plen;
    if (cap < blen + blen / 254 + 3) {
        return 0;
    }
    size_t n = tc_cobs_encode(body, blen, out);
    out[n++] = 0;
    return n;
}

void tc_deframer_init(tc_deframer *d)
{
    memset(d, 0, sizeof(*d));
}

int tc_deframer_push(tc_deframer *d, uint8_t b, tc_frame *f)
{
    if (b != 0) {
        if (d->len >= TC_MAX_RAW) {
            d->overflow = 1;
        } else {
            d->buf[d->len++] = b;
        }
        return 0;
    }
    int ok = 0;
    if (d->overflow) {
        d->errors++;
    } else if (d->len) {
        int n = tc_cobs_decode(d->buf, d->len, d->body);
        if (n < 8 || n > TC_MAX_BODY || tc_crc32(0, d->body, (size_t)n - 4) != tc_get32(d->body + n - 4)) {
            d->errors++;
        } else {
            f->type = d->body[0];
            f->flags = d->body[1];
            f->chan = tc_get16(d->body + 2);
            f->plen = (size_t)n - 8;
            memcpy(f->payload, d->body + 4, f->plen);
            ok = 1;
        }
    }
    d->len = 0;
    d->overflow = 0;
    return ok;
}
