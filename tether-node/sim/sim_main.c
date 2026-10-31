/* Linux simulator of tether-node: the real engine, with a pseudo-terminal as the "USB serial port".
 * Prints "PTY <path>" on stdout; point tether-host's host.port at that path. For tests and development. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

#include "tc_engine.h"

static int master = -1;

static uint32_t sim_millis(void *c)
{
    (void)c;
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)(ts.tv_sec * 1000u + ts.tv_nsec / 1000000u);
}

static int sim_read(void *c, uint8_t *buf, size_t cap)
{
    (void)c;
    ssize_t n = read(master, buf, cap);
    if (n > 0) return (int)n;
    return 0;   /* EAGAIN / EIO (no reader attached): nothing available */
}

static int sim_write(void *c, const uint8_t *buf, size_t n, int may_drop)
{
    (void)c;
    size_t off = 0;
    int waited = 0;
    while (off < n) {
        ssize_t w = write(master, buf + off, n - off);
        if (w > 0) { off += (size_t)w; waited = 0; continue; }
        if (w < 0 && errno != EAGAIN && errno != EIO) return -1;
        if (may_drop) return 0;
        struct pollfd p = {master, POLLOUT, 0};
        poll(&p, 1, 100);
        if (++waited > 50) return -1;   /* 5 s without progress */
    }
    return 0;
}

static uint32_t sim_random(void *c) { (void)c; return (uint32_t)(sim_millis(NULL) * 2654435761u) ^ (uint32_t)getpid(); }
static int sim_wifi(void *c, const tc_wifi_cfg *w, char *err, size_t cap)
{
    (void)c; (void)err; (void)cap;
    fprintf(stderr, "[sim] wifi: %d network(s), first '%s', hotspot %s\n", w->n_nets, w->nets[0].ssid, w->hotspot ? "on" : "off");
    return getenv("SIM_WIFI_FAIL") ? -1 : 0;
}
static int sim_subnet(void *c, uint32_t local, uint32_t *net, uint32_t *mask)
{
    (void)c;
    if ((local >> 24) == 127) { *net = 0x7F000000u; *mask = 0xFF000000u; return 0; }
    return -1;
}
static void sim_status(void *c, tc_status *s)
{
    (void)c;
    s->mode = "sta";
    snprintf(s->ssid, sizeof s->ssid, "sim");
    snprintf(s->ip, sizeof s->ip, "127.0.0.1");
    snprintf(s->hostname, sizeof s->hostname, "tether-sim.local");
    s->rssi = -50;
    s->heap_free = 123456;
}
static void sim_reboot(void *c, int mode) { (void)c; fprintf(stderr, "[sim] reboot mode %d\n", mode); fflush(stderr); exit(0); }
static void sim_log(void *c, int lvl, const char *m) { (void)c; fprintf(stderr, "[node %d] %s\n", lvl, m); }

int main(void)
{
    signal(SIGPIPE, SIG_IGN);
    master = posix_openpt(O_RDWR | O_NOCTTY);
    if (master < 0 || grantpt(master) || unlockpt(master)) { perror("pty"); return 1; }
    const char *path = ptsname(master);
    int slave = open(path, O_RDWR | O_NOCTTY);   /* kept open so the master never sees a hangup */
    struct termios t;
    tcgetattr(slave, &t);
    cfmakeraw(&t);
    tcsetattr(slave, TCSANOW, &t);
    fcntl(master, F_SETFL, fcntl(master, F_GETFL, 0) | O_NONBLOCK);
    printf("PTY %s\n", path);
    fflush(stdout);

    tc_platform p = {0};
    p.version = "tether-sim 0.1";
    p.link_fd = master;
    p.link_read = sim_read;
    p.link_write = sim_write;
    p.millis = sim_millis;
    p.random32 = sim_random;
    p.apply_wifi = sim_wifi;
    p.iface_subnet = sim_subnet;
    p.get_status = sim_status;
    p.reboot = sim_reboot;
    p.log = sim_log;
    tc_engine *e = tc_engine_new(&p);
    while (tc_engine_poll(e, 100) >= 0) {
    }
    tc_engine_free(e);
    return 0;
}
