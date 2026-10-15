#pragma once

// Compile-time defaults. The first-run wizard on the Cardputer stores the
// live values in NVS and those win after you save. You can still fill these
// in so the device connects without typing on the tiny keyboard.

#ifndef WIFI_SSID
#define WIFI_SSID ""
#endif

#ifndef WIFI_PASS
#define WIFI_PASS ""
#endif

#ifndef SERVER_HOST
#define SERVER_HOST "192.168.1.10"
#endif

#ifndef SERVER_PORT
#define SERVER_PORT 8080
#endif

#ifndef API_TOKEN
#define API_TOKEN ""
#endif
