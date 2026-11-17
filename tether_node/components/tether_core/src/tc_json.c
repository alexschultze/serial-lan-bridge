#include "tc_json.h"

#include <stdlib.h>
#include <string.h>

typedef struct {
    const char *js;
    size_t len, pos;
    tj_tok *toks;
    int max, n;
} P;

static void ws(P *p)
{
    while (p->pos < p->len && (p->js[p->pos] == ' ' || p->js[p->pos] == '\t' || p->js[p->pos] == '\n' || p->js[p->pos] == '\r')) {
        p->pos++;
    }
}

static int alloc(P *p, tj_type t, int start)
{
    if (p->n >= p->max) {
        return -2;
    }
    tj_tok *k = &p->toks[p->n];
    k->type = t;
    k->start = start;
    k->end = start;
    k->size = 0;
    return p->n++;
}

static int value(P *p, int depth);

static int string(P *p)
{
    size_t s = ++p->pos;   /* after the opening quote */
    while (p->pos < p->len) {
        char c = p->js[p->pos];
        if (c == '"') {
            int i = alloc(p, TJ_STR, (int)s);
            if (i < 0) {
                return i;
            }
            p->toks[i].end = (int)p->pos;
            p->pos++;
            return i;
        }
        if ((unsigned char)c < 0x20) {
            return -1;
        }
        p->pos += (c == '\\') ? 2 : 1;
    }
    return -1;
}

static int value(P *p, int depth)
{
    if (depth > 8) {
        return -1;
    }
    ws(p);
    if (p->pos >= p->len) {
        return -1;
    }
    char c = p->js[p->pos];
    if (c == '"') {
        return string(p);
    }
    if (c == '{' || c == '[') {
        int i = alloc(p, c == '{' ? TJ_OBJ : TJ_ARR, (int)p->pos), r;
        if (i < 0) {
            return i;
        }
        p->pos++;
        ws(p);
        char close = c == '{' ? '}' : ']';
        if (p->pos < p->len && p->js[p->pos] == close) {
            p->pos++;
            p->toks[i].end = (int)p->pos;
            return i;
        }
        for (;;) {
            if (c == '{') {
                ws(p);
                if (p->pos >= p->len || p->js[p->pos] != '"') {
                    return -1;
                }
                if ((r = string(p)) < 0) {
                    return r;
                }
                p->toks[r].size = 1;
                ws(p);
                if (p->pos >= p->len || p->js[p->pos] != ':') {
                    return -1;
                }
                p->pos++;
            }
            if ((r = value(p, depth + 1)) < 0) {
                return r;
            }
            p->toks[i].size++;
            ws(p);
            if (p->pos >= p->len) {
                return -1;
            }
            if (p->js[p->pos] == ',') {
                p->pos++;
                continue;
            }
            if (p->js[p->pos] == close) {
                p->pos++;
                p->toks[i].end = (int)p->pos;
                return i;
            }
            return -1;
        }
    }
    /* primitive: number, true, false, null */
    size_t s = p->pos;
    while (p->pos < p->len && strchr(",]} \t\r\n", p->js[p->pos]) == NULL) {
        p->pos++;
    }
    if (p->pos == s) {
        return -1;
    }
    int i = alloc(p, TJ_PRIM, (int)s);
    if (i < 0) {
        return i;
    }
    p->toks[i].end = (int)p->pos;
    return i;
}

int tj_parse(const char *js, size_t len, tj_tok *toks, int max)
{
    P p = {js, len, 0, toks, max, 0};
    int r = value(&p, 0);
    if (r < 0) {
        return r;
    }
    ws(&p);
    return p.pos == p.len ? p.n : -1;
}

int tj_skip(const tj_tok *toks, int i)
{
    int j = i + 1;
    if (toks[i].type == TJ_OBJ) {
        for (int k = toks[i].size; k > 0; k--) {
            j = tj_skip(toks, j + 1);   /* j is the key; its value follows */
        }
    } else if (toks[i].type == TJ_ARR) {
        for (int k = toks[i].size; k > 0; k--) {
            j = tj_skip(toks, j);
        }
    }
    return j;
}

int tj_get(const char *js, const tj_tok *toks, int obj, const char *key)
{
    if (obj < 0 || toks[obj].type != TJ_OBJ) {
        return -1;
    }
    size_t kl = strlen(key);
    int i = obj + 1;
    for (int k = toks[obj].size; k > 0; k--) {
        if ((size_t)(toks[i].end - toks[i].start) == kl && memcmp(js + toks[i].start, key, kl) == 0) {
            return i + 1;
        }
        i = tj_skip(toks, i + 1);
    }
    return -1;
}

int tj_item(const tj_tok *toks, int arr, int n)
{
    if (arr < 0 || toks[arr].type != TJ_ARR || n >= toks[arr].size) {
        return -1;
    }
    int i = arr + 1;
    while (n-- > 0) {
        i = tj_skip(toks, i);
    }
    return i;
}

static int hex4(const char *s, unsigned *v)
{
    *v = 0;
    for (int i = 0; i < 4; i++) {
        char c = s[i];
        *v <<= 4;
        if (c >= '0' && c <= '9') *v |= (unsigned)(c - '0');
        else if (c >= 'a' && c <= 'f') *v |= (unsigned)(c - 'a' + 10);
        else if (c >= 'A' && c <= 'F') *v |= (unsigned)(c - 'A' + 10);
        else return -1;
    }
    return 0;
}

int tj_str(const char *js, const tj_tok *t, char *out, size_t cap)
{
    if (t->type != TJ_STR) {
        return -1;
    }
    size_t o = 0;
    for (int i = t->start; i < t->end; i++) {
        unsigned ch = (unsigned char)js[i];
        if (ch == '\\') {
            if (++i >= t->end) return -1;
            switch (js[i]) {
            case 'n': ch = '\n'; break;
            case 't': ch = '\t'; break;
            case 'r': ch = '\r'; break;
            case 'b': ch = '\b'; break;
            case 'f': ch = '\f'; break;
            case 'u':
                if (i + 4 >= t->end) return -1;
                if (hex4(js + i + 1, &ch) != 0) return -1;
                i += 4;
                break;
            default: ch = (unsigned char)js[i]; break;   /* \" \\ \/ */
            }
        }
        unsigned char enc[3];
        size_t el;
        if (ch < 0x80) { enc[0] = (unsigned char)ch; el = 1; }
        else if (ch < 0x800) { enc[0] = (unsigned char)(0xC0 | (ch >> 6)); enc[1] = (unsigned char)(0x80 | (ch & 0x3F)); el = 2; }
        else { enc[0] = (unsigned char)(0xE0 | (ch >> 12)); enc[1] = (unsigned char)(0x80 | ((ch >> 6) & 0x3F)); enc[2] = (unsigned char)(0x80 | (ch & 0x3F)); el = 3; }
        if (o + el + 1 > cap) {
            return -1;
        }
        memcpy(out + o, enc, el);
        o += el;
    }
    if (o + 1 > cap) {
        return -1;
    }
    out[o] = 0;
    return (int)o;
}

int tj_int(const char *js, const tj_tok *t, long *out)
{
    if (t->type != TJ_PRIM || t->end - t->start > 15) {
        return -1;
    }
    char b[16];
    memcpy(b, js + t->start, (size_t)(t->end - t->start));
    b[t->end - t->start] = 0;
    char *e;
    long v = strtol(b, &e, 10);
    if (e == b || *e) {
        return -1;
    }
    *out = v;
    return 0;
}

int tj_bool(const char *js, const tj_tok *t, int *out)
{
    if (t->type != TJ_PRIM) return -1;
    if (t->end - t->start == 4 && memcmp(js + t->start, "true", 4) == 0) { *out = 1; return 0; }
    if (t->end - t->start == 5 && memcmp(js + t->start, "false", 5) == 0) { *out = 0; return 0; }
    return -1;
}

int tj_is_null(const char *js, const tj_tok *t)
{
    return t->type == TJ_PRIM && t->end - t->start == 4 && memcmp(js + t->start, "null", 4) == 0;
}
