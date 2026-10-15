#include <M5Cardputer.h>
#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <SPI.h>
#include <SD.h>
#include <LittleFS.h>
#include <ArduinoJson.h>

static constexpr int SD_SCK = 40, SD_MISO = 39, SD_MOSI = 14, SD_CS = 12;
static constexpr int SAMPLE_RATE = 16000;
static constexpr size_t CHUNK_SAMPLES = 256;
static constexpr uint32_t MAX_AUDIO_BYTES = 4 * 1024 * 1024;

WebServer web(80);
DynamicJsonDocument cfg(8192);
fs::FS* storage = nullptr;
bool sdOK = false;
String statusText = "Booting...";
String lastTranscript, lastAnswer;

const char INDEX_HTML[] PROGMEM = R"HTML(
<!doctype html><html><head><meta name="viewport" content="width=device-width">
<title>Cardputer AI</title><style>
body{font-family:sans-serif;max-width:720px;margin:auto;padding:18px;background:#111;color:#eee}
input,textarea,button{font:inherit;width:100%;box-sizing:border-box;margin:6px 0;padding:10px}
button{cursor:pointer}pre{white-space:pre-wrap;background:#222;padding:10px}
</style></head><body><h2>Cardputer Local AI</h2>
<p id=s>Ready</p><textarea id=t rows=4 placeholder="Type a message"></textarea>
<button onclick=chat()>Send</button><pre id=o></pre>
<hr><h3>Config</h3><textarea id=c rows=18></textarea><button onclick=save()>Save config</button>
<script>
async function chat(){let t=document.getElementById('t').value;let r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t})});let j=await r.json();document.getElementById('o').textContent=j.text||j.error||JSON.stringify(j)}
async function load(){let r=await fetch('/api/config');document.getElementById('c').value=JSON.stringify(await r.json(),null,2)}
async function save(){let x=JSON.parse(document.getElementById('c').value);let r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(x)});document.getElementById('s').textContent=await r.text()}
load()
</script></body></html>
)HTML";

String cfgString(const char* key, const String& fallback="") {
  if (cfg[key].is<const char*>()) return cfg[key].as<String>();
  return fallback;
}
int cfgInt(const char* key, int fallback) {
  return cfg[key].is<int>() ? cfg[key].as<int>() : fallback;
}

bool loadConfig() {
  if (SD.begin(SD_CS, SPI, 25000000)) {
    sdOK = true; storage = &SD;
  } else if (LittleFS.begin(true)) {
    sdOK = false; storage = &LittleFS;
  } else return false;

  if (!storage->exists("/config.json")) {
    cfg["wifi_ssid"] = "YOUR_WIFI";
    cfg["wifi_password"] = "YOUR_PASSWORD";
    cfg["server_host"] = "100.x.y.z";       // Tailscale IPv4 of Ubuntu server
    cfg["server_port"] = 8765;
    cfg["max_record_seconds"] = 60;
    cfg["device_name"] = "cardputer-adv";
    File f = storage->open("/config.json", FILE_WRITE);
    serializeJsonPretty(cfg, f); f.close();
    return true;
  }
  File f = storage->open("/config.json", FILE_READ);
  DeserializationError e = deserializeJson(cfg, f); f.close();
  return !e;
}

bool saveConfig() {
  storage->remove("/config.json");
  File f = storage->open("/config.json", FILE_WRITE);
  if (!f) return false;
  serializeJsonPretty(cfg, f); f.close();
  return true;
}

String baseURL() {
  return "http://" + cfgString("server_host") + ":" + String(cfgInt("server_port",8765));
}

void screen(const String& a, const String& b="") {
  M5Cardputer.Display.fillScreen(TFT_BLACK);
  M5Cardputer.Display.setCursor(4, 4);
  M5Cardputer.Display.setTextSize(1);
  M5Cardputer.Display.println(a);
  if (b.length()) { M5Cardputer.Display.println(); M5Cardputer.Display.println(b); }
}

void writeWavHeader(File& f, uint32_t samples) {
  uint32_t dataBytes=samples*2, fileBytes=36+dataBytes;
  uint8_t h[44] = {
    'R','I','F','F',0,0,0,0,'W','A','V','E','f','m','t',' ',16,0,0,0,
    1,0,1,0,0,0,0,0,0,0,0,0,2,0,16,0,'d','a','t','a',0,0,0,0
  };
  memcpy(h+4,&fileBytes,4); uint32_t sr=SAMPLE_RATE; memcpy(h+24,&sr,4);
  uint32_t byteRate=SAMPLE_RATE*2; memcpy(h+28,&byteRate,4);
  memcpy(h+40,&dataBytes,4);
  f.write(h,44);
}

bool recordToStorage() {
  uint32_t seconds = constrain(cfgInt("max_record_seconds",60), 1, 180);
  uint32_t totalSamples = seconds * SAMPLE_RATE;
  storage->remove("/record.wav");
  File f = storage->open("/record.wav", FILE_WRITE);
  if (!f) return false;
  writeWavHeader(f,0);

  M5Cardputer.Speaker.end();
  M5Cardputer.Mic.begin();
  screen("RECORDING", String(seconds) + " sec max");
  uint32_t done=0;
  int16_t samples[CHUNK_SAMPLES];
  while (done < totalSamples) {
    M5Cardputer.update();
    size_t n = min((uint32_t)CHUNK_SAMPLES, totalSamples-done);
    if (!M5Cardputer.Mic.record(samples, n, SAMPLE_RATE)) continue;
    f.write((uint8_t*)samples, n*2);
    done += n;
    if (M5Cardputer.BtnA.wasClicked()) break;
  }
  M5Cardputer.Mic.end();
  M5Cardputer.Speaker.begin();

  uint32_t dataBytes = done*2, fileBytes=36+dataBytes;
  f.seek(4); f.write((uint8_t*)&fileBytes,4);
  f.seek(24); uint32_t sr=SAMPLE_RATE; f.write((uint8_t*)&sr,4);
  uint32_t br=SAMPLE_RATE*2; f.seek(28); f.write((uint8_t*)&br,4);
  f.seek(40); f.write((uint8_t*)&dataBytes,4);
  f.close();
  return done > SAMPLE_RATE/2;
}

bool uploadVoice(String& answer) {
  File f = storage->open("/record.wav", FILE_READ);
  if (!f) return false;
  HTTPClient http;
  http.begin(baseURL()+"/api/voice");
  http.addHeader("Content-Type","audio/wav");
  int code = http.sendRequest("POST", &f, f.size());
  String body = http.getString();
  f.close(); http.end();
  if (code != 200) { answer = "Voice error HTTP " + String(code) + "\n" + body; return false; }

  DynamicJsonDocument d(8192);
  if (deserializeJson(d, body)) { answer="Bad server JSON"; return false; }
  lastTranscript = d["transcript"].as<String>();
  lastAnswer = d["text"].as<String>();
  answer = lastAnswer;
  return true;
}

bool downloadTTS(const String& text) {
  HTTPClient http;
  http.begin(baseURL()+"/api/tts");
  http.addHeader("Content-Type","application/json");
  DynamicJsonDocument d(4096); d["text"]=text;
  String payload; serializeJson(d,payload);
  int code=http.POST(payload);
  if(code!=200){http.end();return false;}
  storage->remove("/tts.wav");
  File f=storage->open("/tts.wav",FILE_WRITE);
  if(!f){http.end();return false;}
  WiFiClient* s=http.getStreamPtr();
  uint8_t buf[1024]; int left=http.getSize();
  while(http.connected() && (left>0 || left==-1)){
    size_t avail=s->available();
    if(avail){size_t n=s->readBytes(buf,min(avail,sizeof(buf)));f.write(buf,n);if(left>0)left-=n;}
    else delay(1);
  }
  f.close(); http.end(); return true;
}

bool playTTS() {
  File f=storage->open("/tts.wav",FILE_READ);
  if(!f || f.size()>MAX_AUDIO_BYTES){if(f)f.close();return false;}
  size_t n=f.size(); uint8_t* data=(uint8_t*)malloc(n);
  if(!data){f.close();return false;}
  f.read(data,n); f.close();
  M5Cardputer.Mic.end(); M5Cardputer.Speaker.begin();
  screen("AI SPEAKING", lastAnswer.substring(0,80));
  bool ok=M5Cardputer.Speaker.playWav(data,n,1,-1,true);
  while(M5Cardputer.Speaker.isPlaying()){M5Cardputer.update();delay(5);}
  M5Cardputer.Speaker.end(); M5Cardputer.Mic.begin();
  free(data); return ok;
}

void handleChat() {
  if(!web.hasArg("plain")){web.send(400,"text/plain","missing body");return;}
  DynamicJsonDocument d(4096); if(deserializeJson(d,web.arg("plain"))){web.send(400,"text/plain","bad json");return;}
  String text=d["text"].as<String>();
  HTTPClient http; http.begin(baseURL()+"/api/chat"); http.addHeader("Content-Type","application/json");
  int code=http.POST(web.arg("plain")); String body=http.getString(); http.end();
  web.send(code,"application/json",body);
}

void handleConfigGet(){String out;serializeJson(cfg,out);web.send(200,"application/json",out);}
void handleConfigPost(){
  DynamicJsonDocument d(8192); if(deserializeJson(d,web.arg("plain"))){web.send(400,"text/plain","bad json");return;}
  cfg=d; if(!saveConfig()){web.send(500,"text/plain","save failed");return;}
  web.send(200,"text/plain","saved; reboot to apply Wi-Fi/server changes");
}
void handleRoot(){web.send_P(200,"text/html",INDEX_HTML);}
void handleHealth(){web.send(200,"application/json","{\"ok\":true}");}

void connectWiFi() {
  screen("Wi-Fi",cfgString("wifi_ssid"));
  WiFi.mode(WIFI_STA); WiFi.begin(cfgString("wifi_ssid").c_str(),cfgString("wifi_password").c_str());
  for(int i=0;i<40 && WiFi.status()!=WL_CONNECTED;i++){delay(250);}
  if(WiFi.status()==WL_CONNECTED){
    screen("ONLINE",WiFi.localIP().toString());
  } else screen("Wi-Fi failed","edit /config.json");
}

void setupWeb() {
  web.on("/",HTTP_GET,handleRoot);
  web.on("/api/health",HTTP_GET,handleHealth);
  web.on("/api/config",HTTP_GET,handleConfigGet);
  web.on("/api/config",HTTP_POST,handleConfigPost);
  web.on("/api/chat",HTTP_POST,handleChat);
  web.begin();
}

void setup() {
  auto mcfg=M5.config();
  M5Cardputer.begin(mcfg);
  Serial.begin(115200);
  M5Cardputer.Display.setRotation(1);
  if(!loadConfig()){screen("Storage error");while(1)delay(1000);}
  connectWiFi();
  setupWeb();
  M5Cardputer.Display.println("A = talk");
  M5Cardputer.Display.println("Web UI = http://" + WiFi.localIP().toString());
}

void loop() {
  M5Cardputer.update(); web.handleClient();
  if(M5Cardputer.BtnA.wasClicked()){
    if(!recordToStorage()){screen("Recording failed");delay(1000);return;}
    screen("Uploading...","Whisper -> Ollama");
    String ans;
    if(uploadVoice(ans)){
      screen("YOU:",lastTranscript.substring(0,100));
      M5Cardputer.Display.println("\nAI:");
      M5Cardputer.Display.println(ans.substring(0,300));
      delay(700);
      if(downloadTTS(ans)) playTTS();
    } else {screen("Voice request failed",ans.substring(0,160));delay(1500);}
  }
}
