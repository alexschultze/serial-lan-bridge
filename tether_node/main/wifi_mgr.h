#pragma once
#include "tc_engine.h"

void wifi_mgr_init(void);
int wifi_mgr_apply(const tc_wifi_cfg *cfg, char *err, size_t errcap);   /* called from the net task */
void wifi_mgr_listeners(const tc_config *cfg);                          /* mDNS service records */
void wifi_mgr_status(tc_status *st);
int wifi_mgr_led_state(void);                                           /* bit 0: station has an IP, bit 1: hotspot on */
int wifi_mgr_subnet(uint32_t local_ip, uint32_t *net, uint32_t *mask);
