# ESP32 AI Buddy

A local voice assistant: **Ollama** (LLM), **Whisper** (speech-to-text), and **Piper** (text-to-speech) on an Ubuntu server, with an **M5Stack Cardputer ADV** as the mic, speaker, keyboard, and screen.

The Cardputer stays on your Wi-Fi. It never talks to the cloud. It records a short clip, posts it to your server, then plays the spoken reply.

```
Cardputer ADV  --Wi-Fi-->  Ubuntu server :8080
                              |-- Whisper (STT)
                              |-- Ollama  (LLM)
                              `-- Piper   (TTS)
```

## What you need

- M5Stack **Cardputer ADV** (ESP32-S3 Stamp-S3A, ES8311 codec, MEMS mic, speaker)
- A PC or laptop that can flash firmware (Windows is fine; this repo was started from Windows)
- A spare machine for Ubuntu Server (recommended **24.04 LTS**)
- USB stick (8 GB+) for the Ubuntu installer
- Both devices on the **same LAN**
- Disk on the server: **20 GB+** free (models are large)
- RAM: **8 GB** is workable with `llama3.2:3b` + `base.en`; **16 GB** is more comfortable

---

## 1. Install Ubuntu Server

Ubuntu is not assumed to be present. Install it first.

1. On another computer, download **Ubuntu Server 24.04 LTS**:
   [https://ubuntu.com/download/server](https://ubuntu.com/download/server)
2. Write the ISO to a USB stick:
   - Windows: [Rufus](https://rufus.ie/) (dd mode) or [balenaEtcher](https://etcher.balena.io/)
   - macOS/Linux: balenaEtcher or `dd`
3. Boot the target PC from USB (UEFI). Disable Secure Boot only if the installer complains.
4. Installer choices that matter:
   - **Use an entire disk** unless you dual-boot
   - Enable **OpenSSH server**
   - Skip extra snaps
   - Create a user (remember the password)
5. Finish, remove the USB, reboot, then log in on the console or with SSH:

```bash
ssh YOUR_USER@SERVER_IP
```

Find `SERVER_IP` on the console (`ip a`) or your router’s DHCP list. You will type this IP into the Cardputer later.

Optional but useful:

```bash
sudo apt update && sudo apt upgrade -y
sudo timedatectl set-timezone YOUR/TIMEZONE
```

Copy this project onto the server (from your PC):

```bash
# on the Ubuntu server
sudo apt install -y git
git clone <this-repo-url> ~/ESP32-AI-Buddy
cd ~/ESP32-AI-Buddy
```

If there is no git remote yet, copy the folder with `scp -r ESP32-AI-Buddy YOUR_USER@SERVER_IP:~`

---

## 2. Run the server setup script

On the Ubuntu machine, from the project tree:

```bash
cd ~/ESP32-AI-Buddy/server
python3 setup.py
```

The script will:

- Install OS packages (`python3`, `ffmpeg`, build tools, …)
- Install **Ollama** and pull `llama3.2:3b`
- Create `server/.venv` and install Whisper + Piper
- Download the Piper voice `en_US-lessac-medium`
- Write `server/.env`
- Install and start the **`ai-buddy`** systemd service on port **8080**

Useful flags:

```bash
python3 setup.py --model llama3.2:3b --whisper base.en --port 8080
python3 setup.py --skip-systemd --run          # foreground instead of a service
python3 setup.py --token 'a-long-secret'       # optional Bearer token
```

Smaller/faster LLM if RAM is tight: `--model gemma2:2b`  
Better transcription if the CPU can take it: `--whisper small.en`

Check it:

```bash
curl http://127.0.0.1:8080/health
python3 test_client.py "Hello, who are you?"
hostname -I
```

Leave the service running:

```bash
sudo systemctl status ai-buddy
sudo journalctl -u ai-buddy -f
```

If you skipped systemd:

```bash
~/ESP32-AI-Buddy/server/run.sh
```

Open port 8080 only on the LAN if you use a firewall:

```bash
sudo ufw allow OpenSSH
sudo ufw allow from 192.168.0.0/16 to any port 8080 proto tcp
sudo ufw enable
```

---

## 3. Flash the Cardputer ADV

Do this on your development PC (PlatformIO), not on Ubuntu.

### Install PlatformIO

- **VS Code / Cursor**: install the PlatformIO IDE extension, or
- CLI: `pip install platformio`

### USB / download mode (ADV)

The ADV will not flash if it is sitting in normal run mode.

1. Side power switch **OFF**
2. Hold the **G0** button (the button on the Stamp-S3A module)
3. Plug in USB-C (or flip power **ON** while holding G0)
4. Release G0 — the serial port should appear
5. If Windows asks for a driver, use the [CH340](https://www.wch-ic.com/downloads/CH341SER_EXE.html) or the ESP32-S3 WinUSB driver from the board manager docs

### Optional: bake in Wi-Fi

Edit `firmware/include/secrets.h` if you do not want to type on the Cardputer keyboard:

```cpp
#define WIFI_SSID "YourNetwork"
#define WIFI_PASS "YourPassword"
#define SERVER_HOST "192.168.1.50"   // Ubuntu IP from hostname -I
#define SERVER_PORT 8080
#define API_TOKEN ""                 // same as setup.py --token, if you set one
```

### Build and upload

```bash
cd firmware
pio run -t upload
pio device monitor
```

Or use the PlatformIO **Upload** button with the `cardputer-adv` environment.

---

## 4. First conversation

On the Cardputer:

1. If Wi-Fi was left blank, a wizard asks for **SSID**, **password**, **server IP**, **port**, and optional **token**. Type with the keyboard, **Enter** to confirm each field.
2. The top bar should show **WiFi** and the device IP. Green means `/health` succeeded.
3. **Hold the side button (BtnA)** and speak, then release. Up to **3 seconds** is recorded (Stamp-S3A has no PSRAM).
4. Or **type a question and press Enter**. Piper speaks the reply on the speaker (headphones work on the ADV jack).
5. Press **`/`** on an empty draft to reopen settings.

If the top bar is red, the server is unreachable: confirm IP, `sudo systemctl status ai-buddy`, and that the phone/PC and Cardputer share the same subnet (guest Wi-Fi often isolates clients).

---

## Remote access over Tailscale

The Cardputer cannot join a tailnet itself (there is no Tailscale client for ESP32, and the board has no PSRAM). Instead, put the **server** on your tailnet and publish it with **Tailscale Funnel**, which gives it an HTTPS URL the Cardputer can reach from any Wi-Fi. The Cardputer still speaks plain HTTP on your LAN, so home use keeps working.

**1. Set an API token first.** Funnel is reachable from the public internet, so never expose the server without one:

```bash
cd ~/ESP32-AI-Buddy/server
nano .env                      # API_TOKEN=a-long-random-secret
sudo systemctl restart ai-buddy
```

**2. Install Tailscale and publish port 8080 on the server:**

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale funnel --bg 8080
tailscale funnel status        # shows https://YOUR-HOST.YOUR-TAILNET.ts.net
```

HTTPS and Funnel must be enabled for your tailnet in the admin console (DNS page, then Access controls) the first time.

**3. Point the Cardputer at it.** Press `/` on an empty draft to open settings and enter:

- Server host: `YOUR-HOST.YOUR-TAILNET.ts.net`
- Server port: `443` (port 443 switches the firmware to HTTPS)
- API token: the same secret as above

Or set `SERVER_HOST`, `SERVER_PORT 443` and `API_TOKEN` in `firmware/include/secrets.h` and reflash.

Notes: the Cardputer syncs its clock over NTP after joining Wi-Fi, because certificate checks need the right time. `/health` and the web page stay open without a token, while the AI and config endpoints require it. If you need a private-only setup, use a Tailscale subnet router or a WireGuard server on your LAN instead.

---

## How the pieces talk

| Path | Cardputer | Server |
| --- | --- | --- |
| Voice | `POST /api/talk` raw s16le PCM, header `X-Sample-Rate: 16000` | Whisper → Ollama → Piper, body is s16le PCM, headers `X-Transcript` / `X-Reply` |
| Typed chat | `POST /api/chat` JSON `{text, history}` | Ollama JSON `{reply}` |
| Speak a reply | `POST /api/tts` JSON `{text}` | Piper PCM |
| Status | `GET /health` | Ollama / voice files |

Audio on the wire is **16-bit little-endian mono at 16 kHz**, not WAV. That keeps the ESP32-S3 firmware small.

---

## Layout

```
ESP32-AI-Buddy/
  README.md
  server/
    setup.py              # Ubuntu installer
    ai_buddy_server.py    # FastAPI gateway
    requirements.txt
    config.example.env
    test_client.py
  firmware/
    platformio.ini
    include/config.h
    include/secrets.h
    src/main.cpp
```

---

## Tuning and troubleshooting

**Ollama out of memory**  
Use a smaller model (`gemma2:2b`, `qwen2.5:1.5b`) or close other services. Watch `ollama ps`.

**Whisper is slow on CPU**  
`--whisper tiny.en` is much faster and still usable for short commands. A GPU is auto-detected if CUDA is installed (`WHISPER_DEVICE=auto` in `.env`).

**Mic records silence**  
ADV audio goes through the **ES8311**. This project pins Arduino-ESP32 **3.1.x / IDF 5.3** because IDF 5.5 has a known MCLK issue with this codec. Hold the device closer; 3 s max. Built-in speaker is muted while recording.

**No serial port / flash fails**  
Repeat download mode (power OFF, hold G0, power ON). Close other serial monitors. Try another cable (some are charge-only).

**Change the model later**

```bash
ollama pull qwen2.5:3b
sudo nano ~/ESP32-AI-Buddy/server/.env   # OLLAMA_MODEL=qwen2.5:3b
sudo systemctl restart ai-buddy
```

**Re-run setup**  
Existing `server/.env` is not overwritten. Delete it first if you want a clean config. Piper voices are skipped if already downloaded.

---

## Hardware notes (Cardputer ADV)

| Function | Hardware |
| --- | --- |
| MCU | ESP32-S3FN8 (Stamp-S3A), 8 MB flash, **no PSRAM** |
| Display | ST7789V2 240×135 |
| Keyboard | TCA8418, 56 keys |
| Audio | ES8311 codec, MEMS mic, NS4150B + 1 W speaker, 3.5 mm jack |
| Charge | Power switch **ON** while charging |

Because there is no PSRAM, recordings are short on purpose. That is also a better fit for Whisper on a small home server.
