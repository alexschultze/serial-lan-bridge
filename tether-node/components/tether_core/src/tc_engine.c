#include "tc_engine.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <unistd.h>

#include "tc_json.h"

#ifndef MSG_NOSIGNAL
#define MSG_NOSIGNAL 0
#endif
#ifndef TC_RESOLVER_STACK
#define TC_RESOLVER_STACK 0   /* 0: the platform default; the ESP32 build sets a small explicit size */
#endif

#define HOST_SILENCE_MS 10000
#define HELLO_EVERY_MS 1000
#define STATUS_EVERY_MS 5000
#define LINGER_MS 60000
#define CONNECT_LIMIT_MS 15000   /* hard limit for an outbound connect; the host applies its own, shorter timeout */
#define MAX_RESOLVES 4

static const char RESP_503[] = "HTTP/1.1 503 Service Unavailable\r\nRetry-After: 2\r\nContent-Length: 0\r\nConnection: close\r\n\r\n";

typedef struct {
    bool used, outbound, connecting, dns_pending;
    bool eof_sent, eof_recv, shut_done, sock_eof, data_any, linger_on;
    int sock;
    uint16_t id;             /* the channel id used in frames */
    uint16_t lid;            /* listener id (inbound) or forward id (outbound) */
    uint32_t req;            /* resolver request this channel waits for */
    uint32_t credit_out;     /* bytes we may still send to the host */
    int32_t rx_out;          /* bytes the host may still send us */
    uint32_t consumed;       /* bytes written to the socket since the last CREDIT */
    uint8_t *ring;
    uint32_t cap, head, len;
    uint32_t t_open, t_last, t_linger;
} chan_t;

typedef struct {
    bool open;
    int fd;
    int bound_port;
    tc_listener_cfg cfg;
} lst_t;

typedef struct {
    uint32_t crc_errors, rx_overflow, tx_stalls, accepted, refused, opened, open_failed;
} counters_t;

typedef struct {
    struct tc_engine *e;
    uint32_t req;
    uint16_t port;
    char host[64];
    int ok;
    uint32_t addr_be;
} rjob;

struct tc_engine {
    tc_platform p;
    tc_deframer df;
    tc_frame fr;
    uint32_t errs_seen;
    int state;
    bool host_lost, have_cfg, fault;
    uint32_t session;
    tc_config cfg;
    chan_t ch[TC_MAX_CHANNELS];
    lst_t ls[TC_MAX_LISTENERS];
    uint8_t cfgbuf[TC_MAX_CONFIG];
    size_t cfglen;
    uint32_t last_host_rx, last_hello, last_status, boot_ms, log_win_t;
    int log_win_n;
    bool host_seen;
    counters_t cnt;
    uint8_t rd[TC_MAX_PAYLOAD];
    uint8_t in[2048];
    /* resolver threads hand their results over through this small queue */
    pthread_mutex_t rmx;
    rjob *done[8];
    int ndone, inflight;
    uint32_t next_req;
};

static uint32_t now_ms(tc_engine *e) { return e->p.millis(e->p.ctx); }

/* ---- output ---------------------------------------------------------------------------------- */

static int emit(tc_engine *e, uint8_t type, uint16_t chan, const uint8_t *pl, size_t n, int may_drop)
{
    uint8_t out[TC_MAX_RAW + 8];
    size_t l = tc_frame_encode(type, 0, chan, pl, n, out, sizeof out);
    if (!l) {
        return -1;
    }
    if (e->p.link_write(e->p.ctx, out, l, may_drop) < 0) {
        e->cnt.tx_stalls++;
        e->fault = true;
        return -1;
    }
    return 0;
}

static void elog(tc_engine *e, int level, const char *fmt, ...)
{
    char msg[160];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(msg, sizeof msg, fmt, ap);
    va_end(ap);
    if (e->p.log) {
        e->p.log(e->p.ctx, level, msg);
    }
    uint32_t now = now_ms(e);
    if (e->host_seen && now - e->last_host_rx < HOST_SILENCE_MS) {
        if (now - e->log_win_t >= 1000) {
            e->log_win_t = now;
            e->log_win_n = 0;
        }
        if (e->log_win_n++ < 10) {
            uint8_t pl[1 + 160];
            size_t n = strlen(msg);
            pl[0] = (uint8_t)level;
            memcpy(pl + 1, msg, n);
            emit(e, TC_T_LOG, 0, pl, 1 + n, 1);
        }
    }
}

static void send_hello(tc_engine *e, int may_drop)
{
    uint8_t pl[10 + 32];
    size_t vl = e->p.version ? strlen(e->p.version) : 0;
    if (vl > 32) vl = 32;
    pl[0] = TC_PROTO_VERSION;
    pl[1] = TC_ROLE_NODE;
    pl[2] = (uint8_t)e->state;
    tc_put16(pl + 3, TC_MAX_PAYLOAD);
    pl[5] = TC_MAX_CHANNELS;
    tc_put32(pl + 6, e->session);
    if (vl) memcpy(pl + 10, e->p.version, vl);
    emit(e, TC_T_HELLO, 0, pl, 10 + vl, may_drop);
    e->last_hello = now_ms(e);
}

static size_t jesc(char *o, size_t cap, const char *s)
{
    size_t n = 0;
    for (; *s && n + 7 < cap; s++) {
        unsigned char c = (unsigned char)*s;
        if (c == '"' || c == '\\') { o[n++] = '\\'; o[n++] = (char)c; }
        else if (c < 0x20) n += (size_t)snprintf(o + n, cap - n, "\\u%04x", c);
        else o[n++] = (char)c;
    }
    o[n] = 0;
    return n;
}

static int nchannels(const tc_engine *e)
{
    int n = 0;
    for (int i = 0; i < TC_MAX_CHANNELS; i++) n += e->ch[i].used;
    return n;
}

static chan_t *find_chan(tc_engine *e, uint16_t id)
{
    for (int i = 0; i < TC_MAX_CHANNELS; i++) {
        if (e->ch[i].used && e->ch[i].id == id) return &e->ch[i];
    }
    return NULL;
}

static chan_t *free_slot(tc_engine *e)
{
    for (int i = 0; i < TC_MAX_CHANNELS; i++) {
        if (!e->ch[i].used) return &e->ch[i];
    }
    return NULL;
}

static void send_status(tc_engine *e)
{
    tc_status st;
    memset(&st, 0, sizeof st);
    st.mode = "off";
    if (e->p.get_status) {
        e->p.get_status(e->p.ctx, &st);
    }
    char ssid[80], host[96], note[140], js[860];
    jesc(ssid, sizeof ssid, st.ssid);
    jesc(host, sizeof host, st.hostname);
    jesc(note, sizeof note, st.note);
    const char *state = e->state == TC_STATE_RUNNING ? "running" : (e->host_lost ? "host_lost" : "needs_config");
    int n = snprintf(js, sizeof js,
                     "{\"state\":\"%s\",\"mode\":\"%s\",\"ssid\":\"%s\",\"ip\":\"%s\",\"rssi\":%d,\"hostname\":\"%s\","
                     "\"ap_clients\":%d,\"channels\":%d,\"uptime\":%u,\"counters\":{\"crc_errors\":%u,\"rx_overflow\":%u,"
                     "\"tx_stalls\":%u,\"accepted\":%u,\"refused\":%u,\"opened\":%u,\"open_failed\":%u},\"heap_free\":%u,\"note\":\"%s\",\"drive\":\"%s\",\"version\":\"%s\"}",
                     state, st.mode, ssid, st.ip, st.rssi, host, st.ap_clients, nchannels(e),
                     (unsigned)((now_ms(e) - e->boot_ms) / 1000), (unsigned)e->cnt.crc_errors, (unsigned)e->cnt.rx_overflow,
                     (unsigned)e->cnt.tx_stalls, (unsigned)e->cnt.accepted, (unsigned)e->cnt.refused, (unsigned)e->cnt.opened,
                     (unsigned)e->cnt.open_failed, (unsigned)st.heap_free, note, st.drive ? st.drive : "off", e->p.version ? e->p.version : "");
    if (n > 0 && (size_t)n < sizeof js) {
        emit(e, TC_T_STATUS, 0, (const uint8_t *)js, (size_t)n, 1);
    }
    e->last_status = now_ms(e);
}

/* ---- channels ---------------------------------------------------------------------------------- */

static void chan_free(tc_engine *e, chan_t *c, bool abortive)
{
    (void)e;
    if (!c->used) {
        return;
    }
    if (c->sock >= 0) {
        if (abortive) {
            struct linger lg = {1, 0};
            setsockopt(c->sock, SOL_SOCKET, SO_LINGER, &lg, sizeof lg);
        }
        close(c->sock);
    }
    free(c->ring);
    memset(c, 0, sizeof *c);
    c->sock = -1;
}

static void reset_all(tc_engine *e)
{
    for (int i = 0; i < TC_MAX_CHANNELS; i++) {
        chan_free(e, &e->ch[i], true);
    }
}

static void send_close(tc_engine *e, uint16_t id, uint8_t reason)
{
    emit(e, TC_T_CLOSE, id, &reason, 1, 0);
}

static void send_open_res(tc_engine *e, uint16_t id, uint8_t status)
{
    emit(e, TC_T_OPEN_RES, id, &status, 1, 0);
}

static void to_needs_config(tc_engine *e, const char *why)
{
    elog(e, 1, "session reset: %s", why);
    reset_all(e);
    e->state = TC_STATE_NEEDS_CONFIG;
    e->fault = false;
    send_hello(e, 1);
}

static void maybe_finish(tc_engine *e, chan_t *c)
{
    if (c->eof_sent && c->eof_recv && c->shut_done && c->len == 0) {
        chan_free(e, c, false);
    }
}

static void start_linger(tc_engine *e, chan_t *c)
{
    if (!c->linger_on) {
        c->linger_on = true;
        c->t_linger = now_ms(e);
    }
}

static void tune_socket(int fd)
{
    int one = 1;
    fcntl(fd, F_SETFL, fcntl(fd, F_GETFL, 0) | O_NONBLOCK);
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
    setsockopt(fd, SOL_SOCKET, SO_KEEPALIVE, &one, sizeof one);
#ifdef TCP_KEEPIDLE
    int idle = 60, intvl = 10, cnt = 3;
    setsockopt(fd, IPPROTO_TCP, TCP_KEEPIDLE, &idle, sizeof idle);
    setsockopt(fd, IPPROTO_TCP, TCP_KEEPINTVL, &intvl, sizeof intvl);
    setsockopt(fd, IPPROTO_TCP, TCP_KEEPCNT, &cnt, sizeof cnt);
#endif
}

/* Send ring contents to the socket; grant credit back to the host; do the deferred shutdown. */
static void chan_flush(tc_engine *e, chan_t *c)
{
    if (c->connecting || c->dns_pending) {
        return;   /* data waits in the ring until the connection exists */
    }
    while (c->used && c->len > 0) {
        uint32_t run = c->cap - c->head < c->len ? c->cap - c->head : c->len;
        ssize_t n = send(c->sock, c->ring + c->head, run, MSG_NOSIGNAL);
        if (n > 0) {
            c->head = (c->head + (uint32_t)n) % c->cap;
            c->len -= (uint32_t)n;
            c->consumed += (uint32_t)n;
            c->t_last = now_ms(e);
        } else if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)) {
            break;
        } else {
            send_close(e, c->id, TC_CLOSE_RESET);
            chan_free(e, c, true);
            return;
        }
    }
    if (c->used && c->consumed >= e->cfg.window / 2u) {
        uint8_t pl[4];
        tc_put32(pl, c->consumed);
        if (emit(e, TC_T_CREDIT, c->id, pl, 4, 0) == 0) {
            c->rx_out += (int32_t)c->consumed;
            c->consumed = 0;
        }
    }
    if (c->used && c->eof_recv && c->len == 0 && !c->shut_done) {
        shutdown(c->sock, SHUT_WR);
        c->shut_done = true;
    }
    if (c->used) {
        maybe_finish(e, c);
    }
}

static void chan_read(tc_engine *e, chan_t *c)
{
    size_t want = c->credit_out < TC_MAX_PAYLOAD ? c->credit_out : TC_MAX_PAYLOAD;
    ssize_t n = recv(c->sock, e->rd, want, 0);
    if (n > 0) {
        c->credit_out -= (uint32_t)n;
        c->data_any = true;
        c->t_last = now_ms(e);
        emit(e, TC_T_DATA, c->id, e->rd, (size_t)n, 0);
    } else if (n == 0) {
        c->sock_eof = true;
        if (!c->eof_sent) {
            c->eof_sent = true;
            send_close(e, c->id, TC_CLOSE_EOF);
            start_linger(e, c);
        }
        maybe_finish(e, c);
    } else if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
        send_close(e, c->id, TC_CLOSE_RESET);
        chan_free(e, c, true);
    }
}

/* ---- listeners (inbound: clients on the Wi-Fi side) ------------------------------------------------ */

static void lst_close(tc_engine *e, lst_t *l)
{
    if (!l->open) {
        return;
    }
    for (int i = 0; i < TC_MAX_CHANNELS; i++) {
        if (e->ch[i].used && !e->ch[i].outbound && e->ch[i].lid == l->cfg.id) {
            send_close(e, e->ch[i].id, TC_CLOSE_RESET);
            chan_free(e, &e->ch[i], true);
        }
    }
    close(l->fd);
    memset(l, 0, sizeof *l);
    l->fd = -1;
}

static int lst_open(lst_t *l, const tc_listener_cfg *c)
{
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        return errno;
    }
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof sa);
    sa.sin_family = AF_INET;
    sa.sin_addr.s_addr = htonl(INADDR_ANY);
    sa.sin_port = htons(c->port);
    if (bind(fd, (struct sockaddr *)&sa, sizeof sa) != 0 || listen(fd, 4) != 0) {
        int er = errno;
        close(fd);
        return er;
    }
    fcntl(fd, F_SETFL, fcntl(fd, F_GETFL, 0) | O_NONBLOCK);
    socklen_t sl = sizeof sa;
    getsockname(fd, (struct sockaddr *)&sa, &sl);
    l->open = true;
    l->fd = fd;
    memcpy(&l->cfg, c, sizeof l->cfg);
    l->bound_port = ntohs(sa.sin_port);
    return 0;
}

static bool peer_allowed(tc_engine *e, const tc_listener_cfg *l, uint32_t peer, uint32_t local)
{
    if (l->n_allow) {
        for (int i = 0; i < l->n_allow; i++) {
            if ((peer & l->allow[i].mask) == l->allow[i].net) return true;
        }
        return false;
    }
    uint32_t net, mask;
    if (e->p.iface_subnet && e->p.iface_subnet(e->p.ctx, local, &net, &mask) == 0) {
        return (peer & mask) == net;
    }
    return (peer & 0xFFFFFF00u) == (local & 0xFFFFFF00u);
}

static void do_accept(tc_engine *e, lst_t *l)
{
    for (int k = 0; k < 4; k++) {
        struct sockaddr_in sa, la;
        socklen_t sl = sizeof sa, ll = sizeof la;
        int fd = accept(l->fd, (struct sockaddr *)&sa, &sl);
        if (fd < 0) {
            return;
        }
        memset(&la, 0, sizeof la);
        getsockname(fd, (struct sockaddr *)&la, &ll);
        uint32_t peer = ntohl(sa.sin_addr.s_addr), local = ntohl(la.sin_addr.s_addr);
        struct linger rst = {1, 0};
        if (!peer_allowed(e, &l->cfg, peer, local)) {
            e->cnt.refused++;
            elog(e, 1, "listener %u: refused %u.%u.%u.%u (not in the allow-list%s)", l->cfg.id, (unsigned)(peer >> 24),
                 (unsigned)(peer >> 16 & 255), (unsigned)(peer >> 8 & 255), (unsigned)(peer & 255),
                 l->cfg.n_allow ? "" : "; default: same subnet only");
            setsockopt(fd, SOL_SOCKET, SO_LINGER, &rst, sizeof rst);
            close(fd);
            continue;
        }
        chan_t *c = free_slot(e);
        uint8_t *ring = (c && e->state == TC_STATE_RUNNING) ? malloc(e->cfg.window) : NULL;
        if (!ring) {
            e->cnt.refused++;
            elog(e, 1, "listener %u: refused a client: %s", l->cfg.id,
                 e->state != TC_STATE_RUNNING ? "no host / not configured" : (c ? "out of memory" : "all 16 channels busy"));
            if (l->cfg.http) {
                send(fd, RESP_503, sizeof RESP_503 - 1, MSG_NOSIGNAL);
                shutdown(fd, SHUT_WR);
            } else {
                setsockopt(fd, SOL_SOCKET, SO_LINGER, &rst, sizeof rst);
            }
            close(fd);
            continue;
        }
        uint16_t id = 1;   /* lowest id not used by an inbound channel; at most 16 channels exist, so one is free */
        while (find_chan(e, id)) id++;
        tune_socket(fd);
        memset(c, 0, sizeof *c);
        c->used = true;
        c->sock = fd;
        c->id = id;
        c->lid = l->cfg.id;
        c->ring = ring;
        c->cap = e->cfg.window;
        c->credit_out = e->cfg.window;
        c->rx_out = e->cfg.window;
        c->t_open = c->t_last = now_ms(e);
        e->cnt.accepted++;
        uint8_t pl[8];
        tc_put16(pl, l->cfg.id);
        uint32_t nb = htonl(peer);
        memcpy(pl + 2, &nb, 4);
        tc_put16(pl + 6, ntohs(sa.sin_port));
        emit(e, TC_T_ACCEPT, id, pl, 8, 0);
    }
}

/* ---- outbound: the host asks the node to connect to a configured endpoint --------------------------- */

static uint8_t errno_to_status(int er)
{
    return er == ECONNREFUSED ? TC_OPEN_REFUSED : TC_OPEN_UNREACHABLE;
}

static void open_failed(tc_engine *e, chan_t *c, uint8_t status)
{
    static const char *names[] = {"ok", "refused", "unreachable", "name not resolved", "no free channel",
                                  "unknown forward", "bad request", "not ready"};
    e->cnt.open_failed++;
    elog(e, 1, "forward %u: connect failed: %s", c->lid, status < 8 ? names[status] : "?");
    send_open_res(e, c->id, status);
    chan_free(e, c, true);
}

static void start_connect(tc_engine *e, chan_t *c, uint32_t addr_be, uint16_t port)
{
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        open_failed(e, c, TC_OPEN_UNREACHABLE);
        return;
    }
    fcntl(fd, F_SETFL, fcntl(fd, F_GETFL, 0) | O_NONBLOCK);
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof sa);
    sa.sin_family = AF_INET;
    sa.sin_addr.s_addr = addr_be;
    sa.sin_port = htons(port);
    if (connect(fd, (struct sockaddr *)&sa, sizeof sa) != 0 && errno != EINPROGRESS) {
        int er = errno;
        close(fd);
        open_failed(e, c, errno_to_status(er));
        return;
    }
    c->sock = fd;
    c->connecting = true;
    c->t_open = c->t_last = now_ms(e);
}

static void connect_progress(tc_engine *e, chan_t *c)
{
    int er = 0;
    socklen_t sl = sizeof er;
    if (getsockopt(c->sock, SOL_SOCKET, SO_ERROR, &er, &sl) != 0) {
        er = errno;
    }
    if (er != 0) {
        elog(e, 3, "forward %u: connect error %d (%s)", c->lid, er, strerror(er));
        open_failed(e, c, errno_to_status(er));
        return;
    }
    c->connecting = false;
    tune_socket(c->sock);
    e->cnt.opened++;
    elog(e, 3, "forward %u: connected (channel 0x%04x)", c->lid, c->id);
    send_open_res(e, c->id, TC_OPEN_OK);
    chan_flush(e, c);   /* data that arrived while connecting */
}

static void *resolver_main(void *arg)
{
    rjob *j = arg;
    struct addrinfo hints, *res = NULL;
    memset(&hints, 0, sizeof hints);
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    if (getaddrinfo(j->host, NULL, &hints, &res) == 0 && res) {
        j->addr_be = ((struct sockaddr_in *)res->ai_addr)->sin_addr.s_addr;
        j->ok = 1;
    }
    if (res) freeaddrinfo(res);
    tc_engine *e = j->e;
    pthread_mutex_lock(&e->rmx);
    if (e->ndone < 8) {
        e->done[e->ndone++] = j;
        j = NULL;
    }
    pthread_mutex_unlock(&e->rmx);
    free(j);   /* only if the queue was full: the channel then times out */
    if (e->p.wake) e->p.wake(e->p.ctx);
    return NULL;
}

static void start_resolve(tc_engine *e, chan_t *c, const tc_forward_cfg *f)
{
    rjob *j = calloc(1, sizeof *j);
    pthread_t th;
    pthread_attr_t at;
    if (!j || e->inflight >= MAX_RESOLVES) {
        free(j);
        open_failed(e, c, TC_OPEN_DNS);
        return;
    }
    j->e = e;
    j->req = c->req = ++e->next_req;
    j->port = f->port;
    snprintf(j->host, sizeof j->host, "%s", f->host);
    pthread_attr_init(&at);
    pthread_attr_setdetachstate(&at, PTHREAD_CREATE_DETACHED);
    if (TC_RESOLVER_STACK) pthread_attr_setstacksize(&at, TC_RESOLVER_STACK);
    int rc = pthread_create(&th, &at, resolver_main, j);
    pthread_attr_destroy(&at);
    if (rc != 0) {
        free(j);
        open_failed(e, c, TC_OPEN_DNS);
        return;
    }
    e->inflight++;
    c->dns_pending = true;
    c->t_open = now_ms(e);
}

static void resolver_results(tc_engine *e)
{
    for (;;) {
        rjob *j = NULL;
        pthread_mutex_lock(&e->rmx);
        if (e->ndone > 0) j = e->done[--e->ndone];
        pthread_mutex_unlock(&e->rmx);
        if (!j) return;
        e->inflight--;
        chan_t *c = NULL;
        for (int i = 0; i < TC_MAX_CHANNELS; i++) {
            if (e->ch[i].used && e->ch[i].dns_pending && e->ch[i].req == j->req) c = &e->ch[i];
        }
        if (c) {   /* otherwise the channel was closed in the meantime */
            elog(e, 3, "forward %u: '%s' %s", c->lid, j->host, j->ok ? "resolved" : "could not be resolved");
            c->dns_pending = false;
            if (j->ok) start_connect(e, c, j->addr_be, j->port);
            else open_failed(e, c, TC_OPEN_DNS);
        }
        free(j);
    }
}

static void on_open(tc_engine *e, const tc_frame *f)
{
    uint16_t id = f->chan;
    /* payload: forward id u16, and for a universal forward also port u16 and the host name (1..63 bytes) */
    if (f->plen < 2 || f->plen > 2 + 2 + 63 || f->plen == 3 || f->plen == 4 ||
        id <= TC_HOST_CHAN_BASE || id > TC_HOST_CHAN_BASE + TC_MAX_CHANNELS) {
        send_open_res(e, id, TC_OPEN_BAD_REQUEST);
        return;
    }
    if (e->state != TC_STATE_RUNNING) {
        send_open_res(e, id, TC_OPEN_NOT_READY);
        return;
    }
    uint16_t fid = tc_get16(f->payload);
    const tc_forward_cfg *fw = NULL;
    for (int i = 0; i < e->cfg.n_fwd; i++) {
        if (e->cfg.fwd[i].id == fid) fw = &e->cfg.fwd[i];
    }
    if (!fw) {
        send_open_res(e, id, TC_OPEN_UNKNOWN_FORWARD);
        return;
    }
    tc_forward_cfg named;   /* the endpoint of a universal forward, as named by this OPEN */
    if (f->plen > 2 || fw->any) {
        if (!fw->any || f->plen < 5) {   /* only a universal forward takes an endpoint from the message, and it must name one */
            send_open_res(e, id, TC_OPEN_BAD_REQUEST);
            return;
        }
        memset(&named, 0, sizeof named);
        named.id = fid;
        named.port = tc_get16(f->payload + 2);
        size_t hl = f->plen - 4;
        for (size_t k = 0; k < hl; k++) {
            if (f->payload[4 + k] <= 0x20 || f->payload[4 + k] >= 0x7f) {
                send_open_res(e, id, TC_OPEN_BAD_REQUEST);
                return;
            }
        }
        memcpy(named.host, f->payload + 4, hl);
        if (named.port == 0) {
            send_open_res(e, id, TC_OPEN_BAD_REQUEST);
            return;
        }
        fw = &named;
    }
    if (find_chan(e, id)) {
        send_open_res(e, id, TC_OPEN_BAD_REQUEST);
        return;
    }
    chan_t *c = free_slot(e);
    uint8_t *ring = c ? malloc(e->cfg.window) : NULL;
    if (!ring) {
        e->cnt.open_failed++;
        send_open_res(e, id, TC_OPEN_NO_CAPACITY);
        return;
    }
    memset(c, 0, sizeof *c);
    c->used = true;
    c->outbound = true;
    c->sock = -1;
    c->id = id;
    c->lid = fid;
    c->ring = ring;
    c->cap = e->cfg.window;
    c->credit_out = e->cfg.window;
    c->rx_out = e->cfg.window;
    c->t_open = c->t_last = now_ms(e);
    struct in_addr a;
    if (inet_aton(fw->host, &a)) {
        start_connect(e, c, a.s_addr, fw->port);
    } else {
        start_resolve(e, c, fw);
    }
}

/* ---- configuration -------------------------------------------------------------------------- */

static void send_config_res(tc_engine *e, bool ok, const char *wifi, const char *hotspot, const char *lst_json, const char *errors)
{
    char js[700], esc[300];
    jesc(esc, sizeof esc, errors ? errors : "");
    int n = snprintf(js, sizeof js, "{\"ok\":%s,\"wifi\":\"%s\",\"hotspot\":\"%s\",\"listeners\":[%s],\"errors\":[%s%s%s]}",
                     ok ? "true" : "false", wifi, hotspot, lst_json, esc[0] ? "\"" : "", esc, esc[0] ? "\"" : "");
    if (n > 0 && (size_t)n < sizeof js) {
        emit(e, TC_T_CONFIG_RES, 0, (const uint8_t *)js, (size_t)n, 0);
    }
}

static void apply_config(tc_engine *e)
{
    tc_config nc;
    char err[256];
    if (tc_config_parse((const char *)e->cfgbuf, e->cfglen, &nc, err, sizeof err) != 0) {
        elog(e, 0, "config rejected: %s", err);
        e->state = TC_STATE_NEEDS_CONFIG;
        send_config_res(e, false, "unchanged", "unchanged", "", err);
        return;
    }
    bool ok = true;
    char wifi[96] = "ok", lstj[300] = "";
    if (!e->have_cfg || memcmp(&nc.wifi, &e->cfg.wifi, sizeof nc.wifi) != 0) {
        char werr[64] = "";
        if (e->p.apply_wifi && e->p.apply_wifi(e->p.ctx, &nc.wifi, werr, sizeof werr) != 0) {
            snprintf(wifi, sizeof wifi, "error: %s", werr);
            ok = false;
        }
    }
    if (e->have_cfg && nc.window != e->cfg.window) {
        reset_all(e);
    }
    /* close listeners that disappeared or changed */
    for (int i = 0; i < TC_MAX_LISTENERS; i++) {
        lst_t *l = &e->ls[i];
        if (!l->open) continue;
        int keep = 0;
        for (int j = 0; j < nc.n_lst; j++) {
            if (nc.lst[j].id == l->cfg.id && memcmp(&nc.lst[j], &l->cfg, sizeof l->cfg) == 0) keep = 1;
        }
        if (!keep) lst_close(e, l);
    }
    /* outbound channels of forwards that disappeared or changed */
    for (int i = 0; i < TC_MAX_CHANNELS; i++) {
        chan_t *c = &e->ch[i];
        if (!c->used || !c->outbound) continue;
        int keep = 0;
        for (int j = 0; j < nc.n_fwd; j++) {
            for (int k = 0; k < e->cfg.n_fwd; k++) {
                if (nc.fwd[j].id == c->lid && e->cfg.fwd[k].id == c->lid &&
                    memcmp(&nc.fwd[j], &e->cfg.fwd[k], sizeof nc.fwd[j]) == 0) keep = 1;
            }
        }
        if (!keep) {
            send_close(e, c->id, TC_CLOSE_RESET);
            chan_free(e, c, true);
        }
    }
    size_t off = 0;
    for (int j = 0; j < nc.n_lst; j++) {
        lst_t *slot = NULL;
        const char *status = "ok";
        for (int i = 0; i < TC_MAX_LISTENERS; i++) {
            if (e->ls[i].open && e->ls[i].cfg.id == nc.lst[j].id) slot = &e->ls[i];
        }
        if (!slot) {
            for (int i = 0; i < TC_MAX_LISTENERS; i++) {
                if (!e->ls[i].open) { slot = &e->ls[i]; break; }
            }
            int er = lst_open(slot, &nc.lst[j]);
            if (er != 0) {
                status = er == EADDRINUSE ? "port_in_use" : "error";
                ok = false;
            }
        }
        off += (size_t)snprintf(lstj + off, sizeof lstj - off, "%s{\"id\":%u,\"status\":\"%s\"}", j ? "," : "", nc.lst[j].id, status);
    }
    if (!ok && strncmp(wifi, "error", 5) == 0) {
        memcpy(&nc.wifi, &e->cfg.wifi, sizeof nc.wifi);   /* keep the last applied Wi-Fi settings, retry on the next CONFIG */
    }
    memcpy(&e->cfg, &nc, sizeof e->cfg);
    e->have_cfg = true;
    if (e->p.apply_listeners) {
        e->p.apply_listeners(e->p.ctx, &e->cfg);
    }
    e->state = ok ? TC_STATE_RUNNING : TC_STATE_NEEDS_CONFIG;
    e->host_lost = false;
    elog(e, 2, "config applied: %s, %d listener(s), %d forward(s)", ok ? "ok" : "with errors", nc.n_lst, nc.n_fwd);
    send_config_res(e, ok, wifi, nc.wifi.hotspot ? "ok" : "off", lstj, NULL);
    if (ok) {
        send_status(e);
    }
}

/* ---- frames from the host --------------------------------------------------------------------- */

static void handle_frame(tc_engine *e, const tc_frame *f)
{
    e->last_host_rx = now_ms(e);
    e->host_seen = true;
    switch (f->type) {
    case TC_T_HELLO:
        reset_all(e);
        e->state = TC_STATE_NEEDS_CONFIG;
        e->host_lost = false;
        e->cfglen = 0;
        send_hello(e, 0);
        break;
    case TC_T_PING:
        emit(e, TC_T_PONG, 0, f->payload, f->plen, 0);
        break;
    case TC_T_REBOOT:
        if (f->plen == 1 && e->p.reboot) {
            e->p.reboot(e->p.ctx, f->payload[0]);
        }
        break;
    case TC_T_CONFIG:
        if (e->cfglen + f->plen > sizeof e->cfgbuf) {
            e->cfglen = 0;
            send_config_res(e, false, "unchanged", "unchanged", "", "config too large");
            break;
        }
        memcpy(e->cfgbuf + e->cfglen, f->payload, f->plen);
        e->cfglen += f->plen;
        if (!(f->flags & TC_FLAG_MORE)) {
            apply_config(e);
            e->cfglen = 0;
        }
        break;
    case TC_T_OPEN:
        on_open(e, f);
        break;
    case TC_T_DATA:
    case TC_T_CREDIT:
    case TC_T_CLOSE: {
        chan_t *c = find_chan(e, f->chan);
        if (!c) {
            elog(e, 3, "frame type 0x%02x for channel 0x%04x, which is not open", f->type, f->chan);
            if (f->type != TC_T_CLOSE) send_close(e, f->chan, TC_CLOSE_NO_CHANNEL);
            break;
        }
        if (f->type == TC_T_DATA) {
            c->rx_out -= (int32_t)f->plen;
            if (c->rx_out < 0 || f->plen > c->cap - c->len) {
                e->cnt.rx_overflow++;
                elog(e, 0, "channel 0x%04x: host sent more than its credit (protocol error)", c->id);
                send_close(e, c->id, TC_CLOSE_PROTO);
                chan_free(e, c, true);
                break;
            }
            for (size_t i = 0; i < f->plen; i++) {
                c->ring[(c->head + c->len + i) % c->cap] = f->payload[i];
            }
            c->len += (uint32_t)f->plen;
            c->data_any = true;
            chan_flush(e, c);
        } else if (f->type == TC_T_CREDIT) {
            if (f->plen == 4) {
                uint32_t add = tc_get32(f->payload);
                c->credit_out = (c->credit_out + add < c->credit_out) ? 0xFFFFFFFFu : c->credit_out + add;
            }
        } else if (f->plen == 1 && f->payload[0] == TC_CLOSE_EOF) {
            c->eof_recv = true;
            start_linger(e, c);
            chan_flush(e, c);
        } else if (f->plen == 1 && f->payload[0] != TC_CLOSE_NO_CHANNEL) {
            chan_free(e, c, true);   /* RESET or PROTO; also ends a connect or lookup in progress */
        }
        break;
    }
    default:
        break;   /* unknown types are ignored */
    }
}

/* ---- main loop ------------------------------------------------------------------------------- */

tc_engine *tc_engine_new(const tc_platform *plat)
{
    tc_engine *e = calloc(1, sizeof *e);
    if (!e) {
        return NULL;
    }
    e->p = *plat;
    tc_deframer_init(&e->df);
    pthread_mutex_init(&e->rmx, NULL);
    for (int i = 0; i < TC_MAX_CHANNELS; i++) e->ch[i].sock = -1;
    for (int i = 0; i < TC_MAX_LISTENERS; i++) e->ls[i].fd = -1;
    e->session = plat->random32 ? plat->random32(plat->ctx) : 1;
    e->state = TC_STATE_NEEDS_CONFIG;
    e->boot_ms = now_ms(e);
    e->last_hello = e->boot_ms - HELLO_EVERY_MS;
    e->cfg.window = 4096;
    return e;
}

void tc_engine_free(tc_engine *e)
{
    if (!e) return;
    reset_all(e);
    for (int i = 0; i < TC_MAX_LISTENERS; i++) lst_close(e, &e->ls[i]);
    /* resolver threads in flight still hold a pointer to e: the engine is meant to live until the process or chip ends */
    free(e);
}

int tc_engine_state(const tc_engine *e) { return e->state; }

int tc_engine_listener_port(const tc_engine *e, int id)
{
    for (int i = 0; i < TC_MAX_LISTENERS; i++) {
        if (e->ls[i].open && e->ls[i].cfg.id == id) return e->ls[i].bound_port;
    }
    return -1;
}

static int link_input(tc_engine *e)
{
    for (int round = 0; round < 8; round++) {
        int n = e->p.link_read(e->p.ctx, e->in, sizeof e->in);
        if (n < 0) return -1;
        if (n == 0) break;
        for (int i = 0; i < n; i++) {
            if (tc_deframer_push(&e->df, e->in[i], &e->fr)) {
                handle_frame(e, &e->fr);
            }
        }
        if (e->df.errors != e->errs_seen) {
            e->cnt.crc_errors += e->df.errors - e->errs_seen;
            e->errs_seen = e->df.errors;
            elog(e, 1, "bad frame: kind %u, %u raw bytes, decoded %d, crc %08x want %08x, head %02x %02x %02x %02x %02x %02x %02x %02x",
                 e->df.last_err, e->df.last_len, e->df.last_n, (unsigned)e->df.last_crc_got, (unsigned)e->df.last_crc_want,
                 e->df.last_head[0], e->df.last_head[1], e->df.last_head[2], e->df.last_head[3], e->df.last_head[4],
                 e->df.last_head[5], e->df.last_head[6], e->df.last_head[7]);
            if (e->state == TC_STATE_RUNNING) {
                to_needs_config(e, "framing error");
            }
        }
        if (e->fault) break;
    }
    return 0;
}

int tc_engine_poll(tc_engine *e, int timeout_ms)
{
    fd_set rs, ws;
    FD_ZERO(&rs);
    FD_ZERO(&ws);
    int maxfd = -1;
#define ADD(set, fd) do { FD_SET((fd), &(set)); if ((fd) > maxfd) maxfd = (fd); } while (0)
    if (e->p.link_fd >= 0) ADD(rs, e->p.link_fd);
    if (e->p.wake && e->p.wake_fd >= 0) ADD(rs, e->p.wake_fd);
    for (int i = 0; i < TC_MAX_LISTENERS; i++) {
        if (e->ls[i].open) ADD(rs, e->ls[i].fd);
    }
    for (int i = 0; i < TC_MAX_CHANNELS; i++) {
        chan_t *c = &e->ch[i];
        if (!c->used || c->dns_pending) continue;
        if (c->connecting) {
            ADD(ws, c->sock);
            continue;
        }
        if (!c->sock_eof && c->credit_out > 0) ADD(rs, c->sock);
        if (c->len > 0) ADD(ws, c->sock);
    }
    if (timeout_ms > 200) timeout_ms = 200;
    struct timeval tv = {timeout_ms / 1000, (timeout_ms % 1000) * 1000};
    int r = select(maxfd + 1, &rs, &ws, NULL, &tv);
    if (r < 0 && errno != EINTR) {
        return -1;
    }
    if (r < 0) {
        FD_ZERO(&rs);
        FD_ZERO(&ws);
    }
    uint32_t now = now_ms(e);

    if (e->p.wake && e->p.wake_fd >= 0 && FD_ISSET(e->p.wake_fd, &rs)) {
        /* Read once, never in a loop: an ESP-IDF eventfd read always succeeds (it returns 0 when nothing is pending
         * instead of failing with EAGAIN), so "read until empty" never ends there and hung the node. One read clears
         * an eventfd; anything left in a pipe (the Linux simulator) makes select() report it again next time. */
        uint64_t junk;   /* 8 bytes: an eventfd read must be exactly that size */
        (void)!read(e->p.wake_fd, &junk, sizeof junk);
    }
    resolver_results(e);
    if (e->p.link_fd < 0 || FD_ISSET(e->p.link_fd, &rs)) {
        if (link_input(e) < 0) return -1;
    }
    for (int i = 0; i < TC_MAX_LISTENERS; i++) {
        if (e->ls[i].open && FD_ISSET(e->ls[i].fd, &rs)) do_accept(e, &e->ls[i]);
    }
    for (int i = 0; i < TC_MAX_CHANNELS; i++) {
        chan_t *c = &e->ch[i];
        if (!c->used || c->dns_pending || c->sock < 0) continue;
        if (c->connecting) {
            if (FD_ISSET(c->sock, &ws)) connect_progress(e, c);
            continue;
        }
        if (FD_ISSET(c->sock, &ws)) chan_flush(e, c);
        if (c->used && c->sock >= 0 && FD_ISSET(c->sock, &rs)) chan_read(e, c);
    }

    /* timers */
    now = now_ms(e);
    for (int i = 0; i < TC_MAX_CHANNELS; i++) {
        chan_t *c = &e->ch[i];
        if (!c->used) continue;
        if (c->connecting || c->dns_pending) {
            if (now - c->t_open >= CONNECT_LIMIT_MS) {
                elog(e, 3, "forward %u: no connection after %d s", c->lid, CONNECT_LIMIT_MS / 1000);
                open_failed(e, c, TC_OPEN_UNREACHABLE);
            }
        } else if (c->linger_on && now - c->t_linger >= LINGER_MS) {
            elog(e, 3, "channel 0x%04x: half-closed for 60 s, resetting", c->id);
            send_close(e, c->id, TC_CLOSE_RESET);
            chan_free(e, c, true);
        } else if (!c->data_any && e->cfg.never_used_s && now - c->t_open >= e->cfg.never_used_s * 1000u) {
            elog(e, 3, "channel 0x%04x: no data for %u s, closing", c->id, (unsigned)e->cfg.never_used_s);
            send_close(e, c->id, TC_CLOSE_RESET);
            chan_free(e, c, true);
        } else if (e->cfg.idle_s && now - c->t_last >= e->cfg.idle_s * 1000u) {
            send_close(e, c->id, TC_CLOSE_RESET);
            chan_free(e, c, true);
        }
    }
    if (e->state == TC_STATE_RUNNING && now - e->last_host_rx > HOST_SILENCE_MS) {
        e->host_lost = true;   /* the LOG below is not seen by the host (it is gone), only on the node's console */
        to_needs_config(e, "host silent");
    }
    if (e->state == TC_STATE_NEEDS_CONFIG && now - e->last_hello >= HELLO_EVERY_MS) {
        send_hello(e, 1);
    }
    if (e->state == TC_STATE_RUNNING && now - e->last_status >= STATUS_EVERY_MS) {
        send_status(e);
    }
    if (e->fault) {
        to_needs_config(e, "link write stalled");
    }
    return 0;
}
