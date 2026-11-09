/* Unit tests of the portable core. Usage: test_core <path to testvectors/frames.json> */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "tc_engine.h"
#include "tc_json.h"
#include "tc_proto.h"

static int fails, checks;
#define CHECK(c) do { checks++; if (!(c)) { fails++; printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); } } while (0)

static int hexval(char c) { return c <= '9' ? c - '0' : (c | 32) - 'a' + 10; }
static size_t unhex(const char *s, size_t n, uint8_t *out)
{
    for (size_t i = 0; i < n / 2; i++) out[i] = (uint8_t)(hexval(s[2 * i]) * 16 + hexval(s[2 * i + 1]));
    return n / 2;
}

static void test_crc_cobs(void)
{
    CHECK(tc_crc32(0, (const uint8_t *)"123456789", 9) == 0xCBF43926u);
    srand(7);
    for (int len = 0; len < 700; len += (len < 20 ? 1 : 17)) {
        uint8_t in[800], enc[900], dec[800];
        for (int i = 0; i < len; i++) in[i] = (uint8_t)((rand() % 5 == 0) ? 0 : rand());
        size_t n = tc_cobs_encode(in, (size_t)len, enc);
        int bad = 0;
        for (size_t i = 0; i < n; i++) bad |= enc[i] == 0;
        CHECK(!bad);
        CHECK(tc_cobs_decode(enc, n, dec) == len && memcmp(in, dec, (size_t)len) == 0);
    }
    uint8_t ff[254], enc[300], dec[300];
    memset(ff, 0xFF, sizeof ff);
    size_t n = tc_cobs_encode(ff, 254, enc);
    CHECK(tc_cobs_decode(enc, n, dec) == 254);
}

static void test_vectors(const char *path)
{
    FILE *fh = fopen(path, "rb");
    CHECK(fh != NULL);
    if (!fh) return;
    static char js[20000];
    size_t len = fread(js, 1, sizeof js, fh);
    fclose(fh);
    static tj_tok t[512];
    int n = tj_parse(js, len, t, 512);
    CHECK(n > 0 && t[0].type == TJ_ARR);
    int cnt = n > 0 ? t[0].size : 0;
    CHECK(cnt > 5);
    for (int i = 0; i < cnt; i++) {
        int o = tj_item(t, 0, i);
        long type, flags, chan;
        static char payload[4000], wire[4000];
        tj_int(js, &t[tj_get(js, t, o, "type")], &type);
        tj_int(js, &t[tj_get(js, t, o, "flags")], &flags);
        tj_int(js, &t[tj_get(js, t, o, "chan")], &chan);
        int pl = tj_str(js, &t[tj_get(js, t, o, "payload")], payload, sizeof payload);
        int wl = tj_str(js, &t[tj_get(js, t, o, "wire")], wire, sizeof wire);
        static uint8_t pb[2000], wb[2200], out[2200];
        size_t pn = unhex(payload, (size_t)pl, pb), wn = unhex(wire, (size_t)wl, wb);

        tc_deframer d;
        tc_deframer_init(&d);
        static tc_frame f;
        int got = 0;
        for (size_t k = 0; k < wn; k++) got += tc_deframer_push(&d, wb[k], &f);
        CHECK(got == 1 && d.errors == 0);
        CHECK(f.type == type && f.flags == flags && f.chan == chan && f.plen == pn && memcmp(f.payload, pb, pn) == 0);
        size_t en = tc_frame_encode((uint8_t)type, (uint8_t)flags, (uint16_t)chan, pb, pn, out, sizeof out);
        CHECK(en == wn && memcmp(out, wb, wn) == 0);
    }
}

static void test_deframer_faults(void)
{
    uint8_t good[64], bad[64];
    size_t gl = tc_frame_encode(TC_T_PING, 0, 0, (const uint8_t *)"\1\0\0\0", 4, good, sizeof good);
    memcpy(bad, good, gl);
    bad[3] ^= 0x40;
    tc_deframer d;
    tc_frame f;
    tc_deframer_init(&d);
    int got = 0;
    for (size_t i = 0; i < gl; i++) got += tc_deframer_push(&d, bad[i], &f);
    CHECK(got == 0 && d.errors == 1);
    for (size_t i = 0; i < gl; i++) got += tc_deframer_push(&d, good[i], &f);
    CHECK(got == 1);
    for (int i = 0; i < 5000; i++) tc_deframer_push(&d, 1, &f);
    tc_deframer_push(&d, 0, &f);
    CHECK(d.errors == 2);
    uint8_t big[TC_MAX_PAYLOAD + 1], out[2000];
    CHECK(tc_frame_encode(TC_T_DATA, 0, 1, big, sizeof big, out, sizeof out) == 0);
}

static void test_json(void)
{
    const char *js = "{\"a\":[1,2,{\"b\":\"x\\u00e4\\\"\"}],\"c\":true,\"d\":null, \"n\":-12}";
    tj_tok t[32];
    int n = tj_parse(js, strlen(js), t, 32);
    CHECK(n > 0);
    int a = tj_get(js, t, 0, "a");
    CHECK(a > 0 && t[a].type == TJ_ARR && t[a].size == 3);
    int o = tj_item(t, a, 2), b = tj_get(js, t, o, "b");
    char s[16];
    CHECK(tj_str(js, &t[b], s, sizeof s) == 4 && strcmp(s, "x\xC3\xA4\"") == 0);
    int c = tj_get(js, t, 0, "c"), bv = 0;
    CHECK(tj_bool(js, &t[c], &bv) == 0 && bv == 1);
    CHECK(tj_is_null(js, &t[tj_get(js, t, 0, "d")]));
    long v = 0;
    CHECK(tj_int(js, &t[tj_get(js, t, 0, "n")], &v) == 0 && v == -12);
    CHECK(tj_get(js, t, 0, "zzz") == -1);
    CHECK(tj_parse("{\"a\":", 5, t, 32) < 0);
    CHECK(tj_parse("{\"a\":1} x", 9, t, 32) < 0);
    CHECK(tj_parse(js, strlen(js), t, 3) < 0);   /* token overflow */
}

static void test_config(void)
{
    const char *ok = "{\"v\":1,\"wifi\":{\"country\":\"DE\",\"networks\":[{\"ssid\":\"NetworkB\",\"hidden\":false,\"psk\":\"password1\"}]},"
                     "\"hotspot\":{\"mode\":\"auto\",\"ssid\":\"\",\"psk\":\"hotspot-pw\",\"fallback\":30},"
                     "\"listeners\":[{\"id\":1,\"port\":8000,\"http\":true,\"allow\":[\"192.168.1.0/24\",\"10.0.0.7/32\"]},{\"id\":2,\"port\":9000,\"http\":false,\"allow\":[]}],"
                     "\"link\":{\"window\":4096,\"never_used_timeout\":30,\"idle_timeout\":0}}";
    tc_config c;
    char err[256];
    CHECK(tc_config_parse(ok, strlen(ok), &c, err, sizeof err) == 0);
    CHECK(strcmp(c.wifi.country, "DE") == 0 && c.wifi.n_nets == 1 && strcmp(c.wifi.nets[0].ssid, "NetworkB") == 0);
    CHECK(c.wifi.hotspot && c.wifi.hs_mode == 0 && c.wifi.hs_fallback == 30);
    CHECK(c.n_lst == 2 && c.lst[0].port == 8000 && c.lst[0].http && c.lst[0].n_allow == 2 && c.lst[1].http == 0);
    CHECK(c.lst[0].allow[0].net == 0xC0A80100u && c.lst[0].allow[0].mask == 0xFFFFFF00u && c.lst[0].allow[1].mask == 0xFFFFFFFFu);
    CHECK(c.window == 4096 && c.never_used_s == 30 && c.idle_s == 0);
    tc_config c2;
    CHECK(tc_config_parse(ok, strlen(ok), &c2, err, sizeof err) == 0 && memcmp(&c.wifi, &c2.wifi, sizeof c.wifi) == 0);

    const char *no_hotspot = "{\"v\":1,\"wifi\":{\"country\":\"DE\",\"networks\":[{\"ssid\":\"a\"}]},\"hotspot\":null,"
                             "\"listeners\":[{\"id\":1,\"port\":1,\"http\":true,\"allow\":[]}],\"link\":{}}";
    CHECK(tc_config_parse(no_hotspot, strlen(no_hotspot), &c, err, sizeof err) == 0 && !c.wifi.hotspot && c.window == 4096);

    const char *fwd = "{\"v\":1,\"wifi\":{\"country\":\"DE\",\"networks\":[{\"ssid\":\"a\"}]},\"hotspot\":null,\"listeners\":[],"
                      "\"forwards\":[{\"id\":1,\"host\":\"192.168.1.20\",\"port\":80},{\"id\":2,\"host\":\"printer.local\",\"port\":631}],\"link\":{}}";
    CHECK(tc_config_parse(fwd, strlen(fwd), &c, err, sizeof err) == 0);
    CHECK(c.n_lst == 0 && c.n_fwd == 2 && c.fwd[0].id == 1 && c.fwd[0].port == 80 && strcmp(c.fwd[1].host, "printer.local") == 0);

    const char *bad[] = {
        "{\"v\":1,\"wifi\":{\"country\":\"DE\",\"networks\":[{\"ssid\":\"a\"}]},\"listeners\":[],\"forwards\":[]}",
        "{\"v\":1,\"wifi\":{\"country\":\"DE\",\"networks\":[{\"ssid\":\"a\"}]},\"forwards\":[{\"id\":1,\"host\":\"x\",\"port\":0}]}",
        "{\"v\":1,\"wifi\":{\"country\":\"DE\",\"networks\":[{\"ssid\":\"a\"}]},\"forwards\":[{\"id\":1,\"host\":\"x\",\"port\":1},{\"id\":1,\"host\":\"y\",\"port\":2}]}",
        "{\"v\":2}",
        "{\"v\":1,\"wifi\":{\"country\":\"DE\",\"networks\":[]},\"listeners\":[{\"id\":1,\"port\":1}]}",
        "{\"v\":1,\"wifi\":{\"country\":\"DE\",\"networks\":[{\"ssid\":\"a\"}]},\"hotspot\":{\"psk\":\"short\"},\"listeners\":[{\"id\":1,\"port\":1}]}",
        "{\"v\":1,\"wifi\":{\"country\":\"DE\",\"networks\":[{\"ssid\":\"a\"}]},\"listeners\":[{\"id\":1,\"port\":5},{\"id\":1,\"port\":6}]}",
        "{\"v\":1,\"wifi\":{\"country\":\"DE\",\"networks\":[{\"ssid\":\"a\"}]},\"listeners\":[{\"id\":1,\"port\":5,\"allow\":[\"nope\"]}]}",
        "{\"v\":1,\"wifi\":{\"country\":\"DE\",\"networks\":[{\"ssid\":\"a\"}]},\"listeners\":[{\"id\":1,\"port\":5}],\"link\":{\"window\":10}}",
        "not json",
    };
    for (size_t i = 0; i < sizeof bad / sizeof *bad; i++) {
        CHECK(tc_config_parse(bad[i], strlen(bad[i]), &c, err, sizeof err) != 0 && err[0]);
    }
}

int main(int argc, char **argv)
{
    test_crc_cobs();
    if (argc > 1) test_vectors(argv[1]);
    test_deframer_faults();
    test_json();
    test_config();
    printf("%d checks, %d failed\n", checks, fails);
    return fails ? 1 : 0;
}
