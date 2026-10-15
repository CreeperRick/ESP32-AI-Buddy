# Cardputer ADV Local AI

This project turns an **M5Stack Cardputer-Adv** into a Wi-Fi voice terminal for an Ubuntu server running:

- Ollama for the local LLM
- Whisper for speech-to-text
- Piper for text-to-speech
- a small FastAPI bridge that joins the three together

The Cardputer records a WAV file, stores it on microSD when present (LittleFS is the fallback), uploads the WAV to the Ubuntu server, receives the transcript + LLM response, requests Piper audio, stores the returned WAV, and plays it through the Cardputer speaker.

## Architecture

```text
Cardputer ADV
  microphone
      |
      | WAV over Wi-Fi
      v
Ubuntu / FastAPI :8765
      |
      +--> Whisper --> text
      |
      +--> Ollama --> answer
      |
      +--> Piper --> WAV
      |
      v
Cardputer speaker

Tailnet:
  Laptop/phone ----Tailscale----> Ubuntu server
                                  |
                                  +---- Wi-Fi/LAN ----> Cardputer
```

### Important Tailscale detail

The ESP32-S3 in the Cardputer-Adv does **not run the normal Tailscale Linux client**. Therefore the Cardputer is not a native tailnet node.

The recommended setup is to make the **Ubuntu server the tailnet node** and configure the Cardputer to call the server using its Tailscale IPv4 address. This puts the AI API connection behind the tailnet when the Cardputer can route to that address. The Cardputer's own web UI remains a LAN service.

If you need the Cardputer's web UI to be reachable *from another tailnet device*, use a small reverse proxy on the Ubuntu server to the Cardputer's LAN IP. Reserve a DHCP address for the Cardputer first.

## 1. Install Ubuntu

Use a current Ubuntu Server LTS release appropriate for your hardware.

Recommended:
- Ubuntu Server 24.04 LTS or newer supported LTS
- wired Ethernet for the server if possible
- at least 8 GB RAM for a small local model
- more RAM/VRAM if you want a larger Ollama model

During Ubuntu installation:
1. Install OpenSSH Server if you want remote administration.
2. Give the server Internet access for the initial package/model installation.
3. After first boot:

```bash
sudo apt update
sudo apt upgrade -y
```

## 2. Install Ollama + the bridge

Copy `server/setup_local_ai.py` to the Ubuntu server.

Then:

```bash
sudo python3 setup_local_ai.py
```

The script will:
- install Python's virtual-environment tooling
- install Ollama if it is missing
- pull the selected Ollama model
- create `/opt/cardputer-ai/`
- create a FastAPI service on TCP port 8765
- install/start Tailscale
- enable the `cardputer-ai.service` systemd service

The default model is `qwen3:4b`. Change it during setup if your server has enough resources for a larger model.

Check:

```bash
systemctl status ollama
systemctl status cardputer-ai
curl http://127.0.0.1:8765/api/health
```

## 3. Whisper and Piper

The bridge supports two modes.

### Mode A: HTTP services

If your existing Whisper service exposes an OpenAI-compatible transcription endpoint, enter that URL when the setup script asks for it.

For Piper, enter an HTTP endpoint that accepts:

```json
{"text":"hello"}
```

and returns a WAV response.

### Mode B: local CLI

Leave the URLs blank.

Whisper will be invoked approximately as:

```bash
whisper /path/to/audio.wav --output_format txt --output_dir /tmp/... --language en
```

Piper will be invoked approximately as:

```bash
piper --model /path/to/model.onnx --output_file /tmp/answer.wav
```

If your existing Whisper/Piper commands differ, edit:

```text
/opt/cardputer-ai/config.json
```

Then:

```bash
sudo systemctl restart cardputer-ai
```

## 4. Add the Ubuntu server to the tailnet

The setup script installs Tailscale and runs:

```bash
sudo tailscale up --hostname cardputer-ai
```

If it prints an authentication URL, open it and approve the machine.

Find the server's tailnet IPv4:

```bash
tailscale ip -4
```

You will get an address similar to:

```text
100.x.y.z
```

Use that address as `server_host` in the Cardputer configuration.

Tailscale's official Linux installation instructions are available at:
https://tailscale.com/docs/install/linux

## 5. Prepare the Cardputer ADV

Install:
- VS Code
- PlatformIO extension
- a USB-C data cable

Open the `cardputer/` directory as a PlatformIO project.

The project uses the M5Stack Cardputer library and the ESP32-S3 DevKitC board definition. M5Stack's current documentation lists this PlatformIO configuration and the Cardputer library supports Cardputer-ADV.

Build:

```bash
pio run
```

Put the Cardputer into download mode:
1. switch the Cardputer off
2. hold the G0 button
3. connect USB-C
4. release G0
5. upload

```bash
pio run -t upload
```

Serial monitor:

```bash
pio device monitor -b 115200
```

## 6. Configure Wi-Fi and the server

The first boot creates:

```text
/config.json
```

on the microSD card if one is present.

Example:

```json
{
  "wifi_ssid": "MyWiFi",
  "wifi_password": "my-password",
  "server_host": "100.x.y.z",
  "server_port": 8765,
  "max_record_seconds": 60,
  "device_name": "cardputer-adv"
}
```

If there is no SD card, the firmware uses LittleFS instead.

You can edit the config in two ways:
- put the SD card in a computer and edit `/config.json`
- connect to the Cardputer's web UI and use its Config section

After changing Wi-Fi or the server address, reboot the Cardputer.

## 7. Cardputer web UI

After Wi-Fi connects, the display shows the Cardputer's LAN IP.

Open:

```text
http://CARDPUTER_IP/
```

The UI provides:
- a simple text chat box
- live server-backed chat
- a JSON configuration editor
- save-to-storage functionality

For security, do **not** expose the Cardputer's port 80 directly to the public Internet.

## 8. Voice operation

Press **BtnA**.

The firmware:
1. records up to `max_record_seconds`
2. writes `/record.wav`
3. uploads the WAV directly to `/api/voice`
4. Ubuntu runs Whisper
5. Ubuntu sends the transcript to Ollama
6. Ubuntu returns the answer
7. Cardputer requests `/api/tts`
8. Piper generates the response WAV
9. Cardputer plays the WAV

Press BtnA again during recording to stop early.

## 9. Longer recordings

The included firmware deliberately does **not** allocate the entire recording in RAM.

It records in small microphone chunks and writes them directly to the selected filesystem.

With a 16 kHz, 16-bit, mono WAV:

```text
1 second  ~= 32 KB
30 seconds ~= 960 KB
60 seconds ~= 1.92 MB
180 seconds ~= 5.76 MB
```

A microSD card is therefore strongly recommended for long recordings.

The firmware automatically prefers:

```text
microSD -> LittleFS
```

If no SD card is available, LittleFS is used. The included flash partition reserves about 4.5 MB for LittleFS, so the SD card is the better option for recordings longer than roughly two minutes.

## 10. Cardputer-ADV hardware notes

The Cardputer-Adv uses the ESP32-S3 and its microSD interface is:

```text
CS   GPIO 12
MOSI GPIO 14
SCK  GPIO 40
MISO GPIO 39
```

The microphone/audio implementation is handled through M5Cardputer/M5Unified rather than manually driving the codec.

The project pins the PlatformIO ESP32 platform to `6.7.0`. This is intentional: Cardputer-Adv audio has had compatibility problems with newer ESP-IDF/I2S combinations, so avoid changing the platform version until you have tested microphone capture.

## 11. Troubleshooting

### Cardputer says Wi-Fi failed

Edit `/config.json` on the SD card:

```json
"wifi_ssid": "your-network",
"wifi_password": "your-password"
```

Reboot.

### Cardputer connects but voice fails

On Ubuntu:

```bash
curl http://127.0.0.1:8765/api/health
sudo journalctl -u cardputer-ai -f
```

Then test the configured Whisper/Piper commands independently.

### Ollama is not responding

```bash
systemctl status ollama
ollama list
curl http://127.0.0.1:11434/api/tags
```

Try:

```bash
ollama run qwen3:4b
```

### Microphone is silent

Make sure this is really a **Cardputer-Adv**, not an original Cardputer.

Cardputer-Adv uses a different audio codec path. Keep the PlatformIO version pinned as supplied in this project and test the M5Stack microphone example if necessary.

### No SD card

The firmware falls back to LittleFS automatically. Insert a FAT32-formatted microSD card if you want substantially longer recordings.

### TTS does not play

The firmware stores the Piper response at:

```text
/tts.wav
```

and currently limits playback buffering to 4 MB. If Piper produces unusually large WAV files, reduce the TTS response length or use a lower sample rate.

## 12. Optional: make the Cardputer web UI reachable through the tailnet

The simplest safe design is:

```text
tailnet client
     |
     v
Ubuntu Tailscale IP
     |
     v
reverse proxy
     |
     v
Cardputer LAN IP:80
```

Give the Cardputer a DHCP reservation, for example `192.168.1.50`, then use nginx or another reverse proxy on Ubuntu to forward a dedicated path/hostname to it.

Do not port-forward the Cardputer from the public Internet.

## Files

```text
cardputer_local_ai/
├── README.md
├── server/
│   ├── setup_local_ai.py
│   └── app.py
└── cardputer/
    ├── platformio.ini
    ├── partitions.csv
    ├── data/
    │   └── config.json
    └── src/
        └── main.cpp
```

## API

Ubuntu bridge:

```text
GET  /api/health
POST /api/chat
POST /api/voice     Content-Type: audio/wav
POST /api/tts       JSON {"text":"..."}
```

The Cardputer's voice endpoint intentionally accepts a raw WAV body instead of multipart form data. That lets the ESP32 stream a long recording from SD/LittleFS without loading the entire recording into RAM.

## Security

This is designed for a trusted LAN/tailnet, not as a public Internet service.

Before exposing it beyond your LAN:
- add authentication to the FastAPI endpoints
- use HTTPS/Tailscale Serve or a reverse proxy
- do not put Wi-Fi passwords or other secrets in a public Git repository
- consider restricting TCP 8765 to the Tailscale interface and/or LAN
