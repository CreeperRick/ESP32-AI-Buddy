#include <Arduino.h>
#include <FS.h>
#include <HTTPClient.h>
#include <LittleFS.h>
#include <Preferences.h>
#include <SD.h>
#include <SPI.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <time.h>
#include <ArduinoJson.h>
#include <M5Cardputer.h>
#include "config.h"
#include "secrets.h"

// Built-in Mozilla root CA bundle (ships with Arduino-ESP32) for HTTPS verification.
extern const uint8_t rootca_crt_bundle_start[] asm("_binary_x509_crt_bundle_bin_start");

namespace {

Preferences prefs;
String wifiSsid;
String wifiPass;
String serverHost;
uint16_t serverPort = SERVER_PORT;
String apiToken;
String ollamaModel;
String whisperModel;
String systemPrompt;
uint8_t volume = DEFAULT_VOLUME;
uint8_t brightness = DEFAULT_BRIGHTNESS;
uint16_t maxRecordS = DEFAULT_MAX_RECORD_S;

String draft;
String statusLine = "Boot";
String logText;
bool serverOk = false;
bool sdReady = false;
bool lfsReady = false;
uint32_t lastHealthMs = 0;
uint32_t lastConfigMs = 0;
bool recording = false;

constexpr int kMaxHistory = 6;
String histRole[kMaxHistory];
String histText[kMaxHistory];
int histCount = 0;

constexpr uint16_t rgb565(uint8_t r, uint8_t g, uint8_t b) {
  return static_cast<uint16_t>(((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3));
}

const uint16_t COL_BG = TFT_BLACK;
const uint16_t COL_FG = TFT_WHITE;
const uint16_t COL_ACCENT = rgb565(80, 220, 140);
const uint16_t COL_MUTED = rgb565(140, 140, 150);
const uint16_t COL_ERR = rgb565(255, 90, 90);
const uint16_t COL_BAR = rgb565(18, 18, 24);
const uint16_t COL_CHEAT = rgb565(255, 196, 72);

void beep(int freq, int ms) {
  M5Cardputer.Speaker.tone(freq, ms);
}

void applyAudioUi() {
  M5Cardputer.Display.setBrightness(brightness);
  M5Cardputer.Speaker.setVolume(volume);
}

void appendLog(const String& line) {
  logText += line + "\n";
  while (logText.length() > 480) {
    int cut = logText.indexOf('\n');
    if (cut < 0) {
      logText.remove(0, 80);
      break;
    }
    logText.remove(0, cut + 1);
  }
}

void pushHistory(const char* role, const String& text) {
  if (histCount == kMaxHistory) {
    for (int i = 1; i < kMaxHistory; ++i) {
      histRole[i - 1] = histRole[i];
      histText[i - 1] = histText[i];
    }
    histCount--;
  }
  histRole[histCount] = role;
  histText[histCount] = text;
  histCount++;
}

// Port 443 means HTTPS (e.g. Tailscale Funnel: https://host.tailnet.ts.net).
bool useTls() { return serverPort == 443; }

String baseUrl() {
  if (useTls()) return "https://" + serverHost;
  return "http://" + serverHost + ":" + String(serverPort);
}

WiFiClientSecure tlsClient;
WiFiClient plainClient;

// Open an HTTP(S) request to the configured server. One shared client keeps
// heap use low (no PSRAM); callers finish one request before starting the next.
bool beginHttp(HTTPClient& http, const String& path) {
  http.setReuse(false);
  if (useTls()) {
    tlsClient.stop();
#ifdef TLS_INSECURE
    tlsClient.setInsecure();
#else
    tlsClient.setCACertBundle(rootca_crt_bundle_start);
#endif
    return http.begin(tlsClient, baseUrl() + path);
  }
  plainClient.stop();
  return http.begin(plainClient, baseUrl() + path);
}

void applyAuth(HTTPClient& http) {
  if (apiToken.length()) {
    http.addHeader("Authorization", "Bearer " + apiToken);
  }
}

void mergeJson(JsonVariantConst obj) {
  if (obj["wifi_ssid"].is<const char*>()) wifiSsid = obj["wifi_ssid"].as<const char*>();
  if (obj["wifi_pass"].is<const char*>()) wifiPass = obj["wifi_pass"].as<const char*>();
  if (obj["server_host"].is<const char*>()) serverHost = obj["server_host"].as<const char*>();
  if (obj["server_port"].is<int>()) serverPort = obj["server_port"].as<int>();
  if (obj["api_token"].is<const char*>()) apiToken = obj["api_token"].as<const char*>();
  if (obj["ollama_model"].is<const char*>()) ollamaModel = obj["ollama_model"].as<const char*>();
  if (obj["whisper_model"].is<const char*>()) whisperModel = obj["whisper_model"].as<const char*>();
  if (obj["system_prompt"].is<const char*>()) systemPrompt = obj["system_prompt"].as<const char*>();
  if (obj["volume"].is<int>()) volume = constrain(obj["volume"].as<int>(), 0, 255);
  if (obj["brightness"].is<int>()) brightness = constrain(obj["brightness"].as<int>(), 10, 255);
  if (obj["max_record_s"].is<int>()) {
    maxRecordS = constrain(obj["max_record_s"].as<int>(), 3, MAX_RECORD_S_CAP);
  }
}

void fillJson(JsonDocument& doc, bool includeSecrets) {
  doc["wifi_ssid"] = wifiSsid;
  if (includeSecrets) doc["wifi_pass"] = wifiPass;
  doc["server_host"] = serverHost;
  doc["server_port"] = serverPort;
  if (includeSecrets) doc["api_token"] = apiToken;
  doc["volume"] = volume;
  doc["brightness"] = brightness;
  doc["max_record_s"] = maxRecordS;
  if (ollamaModel.length()) doc["ollama_model"] = ollamaModel;
  if (whisperModel.length()) doc["whisper_model"] = whisperModel;
  if (systemPrompt.length()) doc["system_prompt"] = systemPrompt;
}

void loadNvs() {
  prefs.begin("buddy", true);
  wifiSsid = prefs.getString("ssid", WIFI_SSID);
  wifiPass = prefs.getString("pass", WIFI_PASS);
  serverHost = prefs.getString("host", SERVER_HOST);
  serverPort = prefs.getUShort("port", SERVER_PORT);
  apiToken = prefs.getString("token", API_TOKEN);
  volume = prefs.getUChar("vol", DEFAULT_VOLUME);
  brightness = prefs.getUChar("bright", DEFAULT_BRIGHTNESS);
  maxRecordS = prefs.getUShort("rec_s", DEFAULT_MAX_RECORD_S);
  ollamaModel = prefs.getString("llm", "");
  whisperModel = prefs.getString("stt", "");
  systemPrompt = prefs.getString("sys", "");
  prefs.end();
}

void saveNvs() {
  prefs.begin("buddy", false);
  prefs.putString("ssid", wifiSsid);
  prefs.putString("pass", wifiPass);
  prefs.putString("host", serverHost);
  prefs.putUShort("port", serverPort);
  prefs.putString("token", apiToken);
  prefs.putUChar("vol", volume);
  prefs.putUChar("bright", brightness);
  prefs.putUShort("rec_s", maxRecordS);
  prefs.putString("llm", ollamaModel);
  prefs.putString("stt", whisperModel);
  prefs.putString("sys", systemPrompt);
  prefs.end();
}

bool initSd() {
  SPI.begin(SD_SCK, SD_MISO, SD_MOSI, SD_CS);
  sdReady = SD.begin(SD_CS, SPI, 20000000);
  return sdReady;
}

bool loadSdConfig() {
  if (!sdReady || !SD.exists(SD_CONFIG_PATH)) return false;
  File f = SD.open(SD_CONFIG_PATH, FILE_READ);
  if (!f) return false;
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, f);
  f.close();
  if (err) return false;
  mergeJson(doc.as<JsonVariantConst>());
  appendLog("Config: SD card");
  return true;
}

bool saveSdConfig() {
  if (!sdReady) {
    initSd();
  }
  if (!sdReady) return false;
  File f = SD.open(SD_CONFIG_PATH, FILE_WRITE);
  if (!f) return false;
  JsonDocument doc;
  fillJson(doc, true);
  serializeJsonPretty(doc, f);
  f.close();
  return true;
}

void drawScreen() {
  auto& d = M5Cardputer.Display;
  d.fillScreen(COL_BG);
  d.fillRect(0, 0, 240, 18, COL_BAR);
  d.setTextColor(serverOk ? COL_ACCENT : COL_ERR, COL_BAR);
  d.setTextDatum(top_left);
  d.setFont(&fonts::Font0);
  d.setCursor(4, 5);
  String wifi = (WiFi.status() == WL_CONNECTED) ? "WiFi" : "NoWiFi";
  d.printf("Buddy %s %s", wifi.c_str(), statusLine.c_str());

  d.setTextColor(COL_MUTED, COL_BG);
  d.setCursor(4, 20);
  d.printf("G0 talk  Enter cheat  / cfg  %s%s", sdReady ? "SD " : "", lfsReady ? "LFS" : "");

  d.setTextColor(COL_FG, COL_BG);
  int y = 32;
  int start = 0;
  while (start < (int)logText.length() && y < 108) {
    int end = logText.indexOf('\n', start);
    if (end < 0) end = logText.length();
    String line = logText.substring(start, end);
    while (line.length() > 38) {
      d.setCursor(4, y);
      d.print(line.substring(0, 38));
      line.remove(0, 38);
      y += 10;
      if (y >= 108) break;
    }
    if (y < 108) {
      d.setCursor(4, y);
      d.print(line);
      y += 10;
    }
    start = end + 1;
  }

  d.fillRect(0, 118, 240, 17, COL_BAR);
  d.setTextColor(recording ? COL_ERR : COL_CHEAT, COL_BAR);
  d.setCursor(4, 122);
  String shown = draft;
  if (shown.length() > 30) shown = shown.substring(shown.length() - 30);
  d.print(recording ? "REC>" : "CHEAT>");
  d.print(shown);
  d.print("_");
}

void setStatus(const String& s) {
  statusLine = s;
  drawScreen();
}

bool connectWifi() {
  if (wifiSsid.isEmpty()) {
    setStatus("Need WiFi");
    return false;
  }
  setStatus("WiFi...");
  WiFi.mode(WIFI_STA);
  WiFi.begin(wifiSsid.c_str(), wifiPass.c_str());
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_TIMEOUT_MS) {
    delay(250);
    M5Cardputer.update();
  }
  if (WiFi.status() == WL_CONNECTED) {
    if (useTls()) {
      // TLS certificate checks need a correct clock.
      configTime(0, 0, "pool.ntp.org", "time.cloudflare.com");
      uint32_t t0 = millis();
      while (time(nullptr) < 1700000000 && millis() - t0 < 8000) delay(200);
    }
    setStatus(WiFi.localIP().toString());
    return true;
  }
  setStatus("WiFi fail");
  return false;
}

bool checkHealth() {
  if (WiFi.status() != WL_CONNECTED) {
    serverOk = false;
    return false;
  }
  HTTPClient http;
  http.setTimeout(4000);
  if (!beginHttp(http, "/health")) {
    serverOk = false;
    return false;
  }
  int code = http.GET();
  serverOk = (code == 200);
  http.end();
  return serverOk;
}

bool pullServerConfig() {
  if (WiFi.status() != WL_CONNECTED) return false;
  HTTPClient http;
  http.setTimeout(5000);
  if (!beginHttp(http, "/api/config")) return false;
  applyAuth(http);
  int code = http.GET();
  if (code != 200) {
    http.end();
    return false;
  }
  JsonDocument doc;
  deserializeJson(doc, http.getString());
  http.end();
  // Keep local Wi-Fi credentials. Take audio/AI fields from the server.
  String keepSsid = wifiSsid;
  String keepPass = wifiPass;
  String keepHost = serverHost;
  uint16_t keepPort = serverPort;
  mergeJson(doc.as<JsonVariantConst>());
  wifiSsid = keepSsid;
  wifiPass = keepPass;
  if (keepHost.length()) serverHost = keepHost;
  if (keepPort) serverPort = keepPort;
  applyAudioUi();
  return true;
}

bool pushServerConfig() {
  if (WiFi.status() != WL_CONNECTED) return false;
  JsonDocument doc;
  fillJson(doc, true);
  String body;
  serializeJson(doc, body);
  HTTPClient http;
  http.setTimeout(8000);
  if (!beginHttp(http, "/api/config")) return false;
  http.addHeader("Content-Type", "application/json");
  applyAuth(http);
  int code = http.PUT(body);
  http.end();
  return code == 200;
}

void persistAll() {
  saveNvs();
  if (saveSdConfig()) appendLog("Saved SD /buddy.json");
  else if (!sdReady) appendLog("No SD; NVS only");
  if (pushServerConfig()) appendLog("Synced web config");
  applyAudioUi();
}

bool playPcmStream(HTTPClient& http) {
  int len = http.getSize();
  WiFiClient* stream = http.getStreamPtr();
  if (!stream) return false;

  M5Cardputer.Mic.end();
  auto spk = M5Cardputer.Speaker.config();
  spk.sample_rate = SAMPLE_RATE;
  M5Cardputer.Speaker.config(spk);
  M5Cardputer.Speaker.begin();
  M5Cardputer.Speaker.setVolume(volume);

  int16_t chunk[PLAY_CHUNK_SAMPLES];
  int remaining = len;
  uint32_t lastData = millis();
  while (http.connected() && (remaining > 0 || len < 0)) {
    size_t avail = stream->available();
    if (!avail) {
      if (millis() - lastData > 2500) break;
      delay(2);
      if (len > 0 && remaining <= 0) break;
      continue;
    }
    lastData = millis();
    size_t want = sizeof(chunk);
    if (len > 0 && remaining > 0 && (int)want > remaining) want = remaining;
    int n = stream->readBytes(reinterpret_cast<char*>(chunk), want);
    if (n <= 0) break;
    size_t samples = n / 2;
    if (samples) {
      while (!M5Cardputer.Speaker.playRaw(chunk, samples, SAMPLE_RATE, false, 1, 0)) {
        delay(2);
      }
    }
    if (len > 0) remaining -= n;
  }
  while (M5Cardputer.Speaker.isPlaying()) delay(4);
  return true;
}

void speakReply(const String& reply) {
  JsonDocument tts;
  tts["text"] = reply;
  String ttsBody;
  serializeJson(tts, ttsBody);
  HTTPClient ttsHttp;
  ttsHttp.setTimeout(HTTP_TIMEOUT_MS);
  if (!beginHttp(ttsHttp, "/api/tts")) return;
  ttsHttp.addHeader("Content-Type", "application/json");
  applyAuth(ttsHttp);
  const char* keys[] = {"X-Sample-Rate"};
  ttsHttp.collectHeaders(keys, 1);
  int ttsCode = ttsHttp.POST(ttsBody);
  if (ttsCode == 200) playPcmStream(ttsHttp);
  ttsHttp.end();
}

void sendCheat() {
  String text = draft;
  text.trim();
  if (!text.length()) return;
  draft = "";
  appendLog("Cheat: " + text);
  pushHistory("user", text);
  setStatus("Think...");

  JsonDocument doc;
  doc["text"] = text;
  JsonArray hist = doc["history"].to<JsonArray>();
  for (int i = 0; i < histCount - 1; ++i) {
    JsonObject m = hist.add<JsonObject>();
    m["role"] = histRole[i];
    m["content"] = histText[i];
  }
  String body;
  serializeJson(doc, body);

  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  if (!beginHttp(http, "/api/chat")) {
    setStatus("HTTP fail");
    return;
  }
  http.addHeader("Content-Type", "application/json");
  applyAuth(http);
  int code = http.POST(body);
  String reply;
  if (code == 200) {
    JsonDocument out;
    deserializeJson(out, http.getString());
    reply = out["reply"].as<String>();
  } else {
    reply = "Chat error " + String(code);
  }
  http.end();

  appendLog("AI: " + reply);
  pushHistory("assistant", reply);
  setStatus("Speak...");
  speakReply(reply);
  setStatus(WiFi.localIP().toString());
  drawScreen();
}

bool pttHeld() {
  return digitalRead(PTT_PIN) == LOW;
}

bool recordAndTalk() {
  if (!lfsReady) {
    setStatus("No LittleFS");
    beep(400, 80);
    return false;
  }

  const int frameSamples = SAMPLE_RATE * RECORD_FRAME_MS / 1000;
  int16_t* frame = (int16_t*)heap_caps_malloc(frameSamples * sizeof(int16_t), MALLOC_CAP_8BIT);
  if (!frame) {
    setStatus("No RAM");
    return false;
  }

  LittleFS.remove(LFS_REC_PATH);
  File rec = LittleFS.open(LFS_REC_PATH, FILE_WRITE);
  if (!rec) {
    heap_caps_free(frame);
    setStatus("LFS write");
    return false;
  }

  size_t freeb = LittleFS.totalBytes() - LittleFS.usedBytes();
  size_t cap = (size_t)maxRecordS * SAMPLE_RATE * 2;
  if (freeb > 65536 && cap > freeb - 65536) cap = freeb - 65536;

  M5Cardputer.Speaker.end();
  auto micCfg = M5Cardputer.Mic.config();
  micCfg.sample_rate = SAMPLE_RATE;
  M5Cardputer.Mic.config(micCfg);
  M5Cardputer.Mic.begin();

  recording = true;
  setStatus("PTT REC");
  beep(1200, 40);

  uint32_t started = millis();
  size_t written = 0;
  while (written + frameSamples * 2 <= cap) {
    if (!M5Cardputer.Mic.record(frame, frameSamples, SAMPLE_RATE)) break;
    rec.write(reinterpret_cast<uint8_t*>(frame), frameSamples * sizeof(int16_t));
    written += frameSamples * sizeof(int16_t);
    M5Cardputer.update();
    uint32_t elapsed = millis() - started;
    if (elapsed > 250 && !pttHeld()) break;
    if (elapsed / 1000 != (elapsed - RECORD_FRAME_MS) / 1000) {
      statusLine = "REC " + String(elapsed / 1000) + "s";
      drawScreen();
    }
  }
  rec.close();
  heap_caps_free(frame);
  recording = false;
  M5Cardputer.Mic.end();
  beep(900, 40);

  if (written < SAMPLE_RATE * 2 / 5) {
    setStatus("Too short");
    drawScreen();
    return false;
  }

  setStatus("Talk...");
  File body = LittleFS.open(LFS_REC_PATH, FILE_READ);
  if (!body) {
    setStatus("LFS read");
    return false;
  }

  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  if (!beginHttp(http, "/api/talk")) {
    body.close();
    setStatus("HTTP fail");
    return false;
  }
  http.addHeader("Content-Type", "application/octet-stream");
  http.addHeader("X-Sample-Rate", String(SAMPLE_RATE));
  applyAuth(http);
  const char* keys[] = {"X-Transcript", "X-Reply", "X-Sample-Rate"};
  http.collectHeaders(keys, 3);
  int code = http.sendRequest("POST", &body, body.size());
  body.close();

  if (code != 200) {
    http.end();
    setStatus("Talk err " + String(code));
    return false;
  }

  String transcript = http.header("X-Transcript");
  String reply = http.header("X-Reply");
  if (transcript.length()) {
    appendLog("You: " + transcript);
    pushHistory("user", transcript);
  }
  if (reply.length()) {
    appendLog("AI: " + reply);
    pushHistory("assistant", reply);
  }
  drawScreen();
  playPcmStream(http);
  http.end();
  LittleFS.remove(LFS_REC_PATH);
  setStatus(WiFi.localIP().toString());
  drawScreen();
  return true;
}

String promptLine(const char* label, const String& current, bool secret = false) {
  String value = current;
  auto& d = M5Cardputer.Display;
  while (true) {
    d.fillScreen(COL_BG);
    d.setFont(&fonts::Font0);
    d.setTextColor(COL_ACCENT, COL_BG);
    d.setCursor(6, 8);
    d.print(label);
    d.setTextColor(COL_MUTED, COL_BG);
    d.setCursor(6, 24);
    d.print("Enter=OK  `=clear  G0=skip");
    d.setTextColor(COL_FG, COL_BG);
    d.setCursor(6, 52);
    if (secret && value.length()) {
      String dots;
      for (size_t i = 0; i < value.length(); ++i) dots += '*';
      d.print(dots);
    } else {
      String shown = value;
      if (shown.length() > 36) shown = shown.substring(shown.length() - 36);
      d.print(shown);
      d.print("_");
    }
    M5Cardputer.update();
    if (digitalRead(PTT_PIN) == LOW) {
      delay(200);
      return current;
    }
    if (M5Cardputer.Keyboard.isChange() && M5Cardputer.Keyboard.isPressed()) {
      Keyboard_Class::KeysState keys = M5Cardputer.Keyboard.keysState();
      if (keys.enter) {
        beep(1000, 20);
        return value;
      }
      if (keys.del && value.length()) value.remove(value.length() - 1);
      for (auto ch : keys.word) {
        if (ch == '`') value = "";
        else value += ch;
      }
    }
    delay(12);
  }
}

void settingsMenu() {
  appendLog("Config editor");
  drawScreen();
  wifiSsid = promptLine("WiFi SSID", wifiSsid);
  wifiPass = promptLine("WiFi password", wifiPass, true);
  serverHost = promptLine("Server host/IP", serverHost);
  String portStr = promptLine("Server port", String(serverPort));
  uint16_t p = portStr.toInt();
  if (p) serverPort = p;
  apiToken = promptLine("API token", apiToken, true);
  String volStr = promptLine("Volume 0-255", String(volume));
  volume = constrain(volStr.toInt(), 0, 255);
  String recStr = promptLine("Max record seconds", String(maxRecordS));
  maxRecordS = constrain(recStr.toInt(), 3, MAX_RECORD_S_CAP);
  String brStr = promptLine("Brightness 10-255", String(brightness));
  brightness = constrain(brStr.toInt(), 10, 255);
  ollamaModel = promptLine("Ollama model", ollamaModel.length() ? ollamaModel : "llama3.2:3b");
  whisperModel = promptLine("Whisper model", whisperModel.length() ? whisperModel : "base.en");
  persistAll();
  connectWifi();
  checkHealth();
  pullServerConfig();
  drawScreen();
}

void handleKeys() {
  if (!M5Cardputer.Keyboard.isChange() || !M5Cardputer.Keyboard.isPressed()) return;
  Keyboard_Class::KeysState keys = M5Cardputer.Keyboard.keysState();
  if (keys.enter) {
    sendCheat();
    return;
  }
  if (keys.del && draft.length()) {
    draft.remove(draft.length() - 1);
    drawScreen();
  }
  for (auto ch : keys.word) {
    if ((ch == '/' || ch == '\\') && draft.length() == 0) {
      settingsMenu();
      return;
    }
    if (ch >= 32 && ch < 127) {
      if (draft.length() < 200) draft += ch;
    }
  }
  drawScreen();
}

}  // namespace

void setup() {
  auto cfg = M5.config();
  cfg.output_power = true;
  M5Cardputer.begin(cfg, true);
  M5Cardputer.Display.setRotation(1);
  pinMode(38, OUTPUT);
  digitalWrite(38, HIGH);
  pinMode(PTT_PIN, INPUT_PULLUP);

  auto spk = M5Cardputer.Speaker.config();
  spk.sample_rate = SAMPLE_RATE;
  M5Cardputer.Speaker.config(spk);
  M5Cardputer.Speaker.begin();

  lfsReady = LittleFS.begin(true, "/littlefs", 10, "spiffs");
  if (!lfsReady) lfsReady = LittleFS.begin(true);

  initSd();
  loadNvs();
  loadSdConfig();
  applyAudioUi();

  appendLog("Hold G0 (Stamp button) to talk.");
  appendLog("Type + Enter = cheat (no mic).");
  appendLog("/ settings  SD=/buddy.json");
  if (sdReady) appendLog("SD card ready.");
  if (lfsReady) {
    appendLog("LittleFS " + String((LittleFS.totalBytes() - LittleFS.usedBytes()) / 1024) + " KB free");
  }
  drawScreen();

  if (wifiSsid.isEmpty()) settingsMenu();
  connectWifi();
  checkHealth();
  if (pullServerConfig()) appendLog("Config: web UI");
  lastHealthMs = lastConfigMs = millis();
  drawScreen();
  beep(1400, 50);
}

void loop() {
  M5Cardputer.update();
  handleKeys();

  if (!recording && pttHeld()) {
    recordAndTalk();
  }

  if (millis() - lastHealthMs > STATUS_POLL_MS) {
    lastHealthMs = millis();
    checkHealth();
    if (WiFi.status() != WL_CONNECTED) connectWifi();
  }
  if (millis() - lastConfigMs > CONFIG_POLL_MS) {
    lastConfigMs = millis();
    pullServerConfig();
    drawScreen();
  }
  delay(8);
}
