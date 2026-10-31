#include <arpa/inet.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "tc_engine.h"
#include "tc_json.h"

typedef struct {
    char *err;
    size_t cap, len;
    int bad;
} Errs;

static void bad(Errs *e, const char *fmt, ...)
{
    va_list ap;
    e->bad++;
    if (e->len + 2 >= e->cap) {
        return;
    }
    va_start(ap, fmt);
    int n = vsnprintf(e->err + e->len, e->cap - e->len, fmt, ap);
    va_end(ap);
    if (n > 0) {
        e->len += (size_t)n;
        if (e->len + 2 < e->cap) {
            e->err[e->len++] = ';';
            e->err[e->len] = 0;
        }
    }
}

static int get_int(const char *js, const tj_tok *t, int obj, const char *key, long def, long lo, long hi, Errs *er, const char *name, long *out)
{
    int v = tj_get(js, t, obj, key);
    long x = def;
    if (v >= 0 && (tj_int(js, &t[v], &x) != 0 || x < lo || x > hi)) {
        bad(er, "%s must be an integer %ld..%ld", name, lo, hi);
        x = def;
    }
    *out = x;
    return v >= 0;
}

static int parse_cidr(const char *s, tc_cidr *c)
{
    char buf[24];
    if (strlen(s) >= sizeof buf) {
        return -1;
    }
    strcpy(buf, s);
    char *slash = strchr(buf, '/');
    int bits = 32;
    if (slash) {
        *slash++ = 0;
        char *e;
        long b = strtol(slash, &e, 10);
        if (*e || e == slash || b < 0 || b > 32) {
            return -1;
        }
        bits = (int)b;
    }
    struct in_addr a;
    if (inet_pton(AF_INET, buf, &a) != 1) {
        return -1;
    }
    c->mask = bits == 0 ? 0 : 0xFFFFFFFFu << (32 - bits);
    c->net = ntohl(a.s_addr) & c->mask;
    return 0;
}

int tc_config_parse(const char *js, size_t len, tc_config *o, char *errbuf, size_t errcap)
{
    static tj_tok t[256];
    Errs er = {errbuf, errcap, 0, 0};
    if (errcap) errbuf[0] = 0;
    memset(o, 0, sizeof *o);
    int n = tj_parse(js, len, t, 256);
    if (n < 1 || t[0].type != TJ_OBJ) {
        bad(&er, "not valid JSON (or too large)");
        return -1;
    }
    long v;
    int ve = tj_get(js, t, 0, "v");
    if (ve < 0 || tj_int(js, &t[ve], &v) != 0 || v != 1) {
        bad(&er, "unsupported config version");
        return -1;
    }

    /* wifi */
    int w = tj_get(js, t, 0, "wifi");
    if (w < 0) {
        bad(&er, "wifi missing");
    } else {
        char cc[8] = "";
        int c = tj_get(js, t, w, "country");
        if (c < 0 || tj_str(js, &t[c], cc, sizeof cc) != 2) {
            bad(&er, "wifi.country must be two letters");
        } else {
            memcpy(o->wifi.country, cc, 3);
        }
        int nets = tj_get(js, t, w, "networks");
        int cnt = nets >= 0 && t[nets].type == TJ_ARR ? t[nets].size : 0;
        if (cnt < 1 || cnt > TC_MAX_NETWORKS) {
            bad(&er, "wifi.networks needs 1..%d entries", TC_MAX_NETWORKS);
        } else {
            for (int i = 0; i < cnt; i++) {
                int it = tj_item(t, nets, i);
                tc_net *nn = &o->wifi.nets[i];
                int s = tj_get(js, t, it, "ssid"), p = tj_get(js, t, it, "psk"), h = tj_get(js, t, it, "hidden");
                int sl = s >= 0 ? tj_str(js, &t[s], nn->ssid, sizeof nn->ssid) : -1;
                if (sl < 1) bad(&er, "network %d: ssid must be 1..32 bytes", i);
                if (p >= 0) {
                    int pl = tj_str(js, &t[p], nn->psk, sizeof nn->psk);
                    if (pl != 0 && (pl < 8 || pl > 63)) bad(&er, "network %d: psk must be 8..63 characters", i);
                }
                int hv = 0;
                if (h >= 0 && tj_bool(js, &t[h], &hv) == 0) nn->hidden = hv;
            }
            o->wifi.n_nets = (uint8_t)cnt;
        }
    }

    /* hotspot */
    int hs = tj_get(js, t, 0, "hotspot");
    if (hs >= 0 && !tj_is_null(js, &t[hs])) {
        char mode[12] = "auto";
        int m = tj_get(js, t, hs, "mode");
        if (m >= 0 && tj_str(js, &t[m], mode, sizeof mode) < 0) strcpy(mode, "?");
        if (strcmp(mode, "never") == 0) {
            /* no hotspot */
        } else if (strcmp(mode, "auto") != 0 && strcmp(mode, "always") != 0) {
            bad(&er, "hotspot.mode must be auto, always or never");
        } else {
            o->wifi.hotspot = true;
            o->wifi.hs_mode = strcmp(mode, "always") == 0;
            int s = tj_get(js, t, hs, "ssid"), p = tj_get(js, t, hs, "psk");
            if (s >= 0 && tj_str(js, &t[s], o->wifi.hs_ssid, sizeof o->wifi.hs_ssid) < 0) bad(&er, "hotspot.ssid too long");
            int pl = p >= 0 ? tj_str(js, &t[p], o->wifi.hs_psk, sizeof o->wifi.hs_psk) : -1;
            if (pl < 8) bad(&er, "hotspot.psk must be 8..63 characters");
            long fb;
            get_int(js, t, hs, "fallback", 30, 0, 3600, &er, "hotspot.fallback", &fb);
            o->wifi.hs_fallback = (uint16_t)fb;
        }
    }

    /* listeners */
    int ls = tj_get(js, t, 0, "listeners");
    int lc = ls >= 0 && t[ls].type == TJ_ARR ? t[ls].size : 0;
    if (lc < 1 || lc > TC_MAX_LISTENERS) {
        bad(&er, "listeners needs 1..%d entries", TC_MAX_LISTENERS);
    } else {
        for (int i = 0; i < lc; i++) {
            int it = tj_item(t, ls, i);
            tc_listener_cfg *l = &o->lst[i];
            long id, port;
            get_int(js, t, it, "id", 0, 1, 65535, &er, "listener id", &id);
            get_int(js, t, it, "port", 0, 0, 65535, &er, "listener port", &port);
            if (id == 0) bad(&er, "listener %d: id required", i);
            l->id = (uint16_t)id;
            l->port = (uint16_t)port;
            int h = tj_get(js, t, it, "http"), hv = 1;
            if (h >= 0) tj_bool(js, &t[h], &hv);
            l->http = hv != 0;
            for (int j = 0; j < i; j++) {
                if (o->lst[j].id == l->id) bad(&er, "listener id %d used twice", l->id);
                if (l->port && o->lst[j].port == l->port) bad(&er, "listener port %d used twice", l->port);
            }
            int al = tj_get(js, t, it, "allow");
            int ac = al >= 0 && t[al].type == TJ_ARR ? t[al].size : 0;
            if (ac > TC_MAX_ALLOW) {
                bad(&er, "listener %d: at most %d allow entries", l->id, TC_MAX_ALLOW);
                ac = TC_MAX_ALLOW;
            }
            for (int k = 0; k < ac; k++) {
                char cs[32];
                int a = tj_item(t, al, k);
                if (tj_str(js, &t[a], cs, sizeof cs) < 0 || parse_cidr(cs, &l->allow[k]) != 0) {
                    bad(&er, "listener %d: bad allow entry", l->id);
                }
            }
            l->n_allow = (uint8_t)ac;
        }
        o->n_lst = (uint8_t)lc;
    }

    /* link */
    int lk = tj_get(js, t, 0, "link");
    long win = 4096, nu = 30, idle = 0;
    if (lk >= 0) {
        get_int(js, t, lk, "window", 4096, 1024, 16384, &er, "link.window", &win);
        get_int(js, t, lk, "never_used_timeout", 30, 0, 3600, &er, "link.never_used_timeout", &nu);
        get_int(js, t, lk, "idle_timeout", 0, 0, 86400, &er, "link.idle_timeout", &idle);
    }
    o->window = (uint16_t)win;
    o->never_used_s = (uint32_t)nu;
    o->idle_s = (uint32_t)idle;
    return er.bad ? -1 : 0;
}
