#pragma once

/* Status LED: red = no host program on the COM port, blue pulse = host up and Wi-Fi not joined yet, green = Wi-Fi joined,
 * cyan = hotspot only, short white flash = data moving over the USB link. */
void led_init(void);        /* starts the LED task; does nothing when TETHER_LED_ENABLE is off */
void led_activity(void);    /* call when payload data crossed the USB link (any task) */
