#pragma once

#define SAMPLE_RATE 16000
#define RECORD_FRAME_MS 200
#define PLAY_CHUNK_SAMPLES 512
#define HTTP_TIMEOUT_MS 180000
#define WIFI_TIMEOUT_MS 20000
#define STATUS_POLL_MS 15000
#define CONFIG_POLL_MS 30000

#define PTT_PIN 0
#define SD_CS 12
#define SD_SCK 40
#define SD_MISO 39
#define SD_MOSI 14
#define SD_CONFIG_PATH "/buddy.json"
#define LFS_REC_PATH "/rec.pcm"

#define DEFAULT_VOLUME 180
#define DEFAULT_BRIGHTNESS 80
#define DEFAULT_MAX_RECORD_S 45
#define MAX_RECORD_S_CAP 120
