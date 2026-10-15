#!/usr/bin/env python3
"""LAN voice+text gateway for Cardputer ADV: Whisper, Ollama, Piper."""

from __future__ import annotations

import io
import json
import os
import threading
import wave
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import soundfile as sf
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from faster_whisper import WhisperModel

try:
    from piper import PiperVoice
except ImportError:
    from piper.voice import PiperVoice

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

CONFIG_PATH = ROOT / "config.json"
STATIC_DIR = ROOT / "static"
TARGET_RATE = 16000
LOCK = threading.Lock()

DEFAULTS: dict[str, Any] = {
    "host": os.getenv("HOST", "0.0.0.0"),
    "port": int(os.getenv("PORT", "8080")),
    "api_token": os.getenv("API_TOKEN", "").strip(),
    "ollama_url": os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/"),
    "ollama_model": os.getenv("OLLAMA_MODEL", "llama3.2:3b"),
    "whisper_model": os.getenv("WHISPER_MODEL", "base.en"),
    "whisper_device": os.getenv("WHISPER_DEVICE", "auto"),
    "piper_voice": os.getenv("PIPER_VOICE", "en_US-lessac-medium"),
    "piper_voices_dir": os.getenv("PIPER_VOICES_DIR", str(ROOT / "voices")),
    "system_prompt": os.getenv(
        "SYSTEM_PROMPT",
        "You are Buddy, a helpful local AI on a tiny M5Stack Cardputer ADV. "
        "Reply in 1-3 short spoken sentences. No markdown, no lists, no emoji unless asked.",
    ),
    "max_history": int(os.getenv("MAX_HISTORY", "8")),
    "max_reply_chars": int(os.getenv("MAX_REPLY_CHARS", "400")),
    "volume": 180,
    "brightness": 80,
    "max_record_s": 45,
    "wifi_ssid": "",
    "wifi_pass": "",
    "server_host": "",
    "server_port": int(os.getenv("PORT", "8080")),
}

CFG: dict[str, Any] = dict(DEFAULTS)
_whisper: WhisperModel | None = None
_piper: PiperVoice | None = None
_whisper_key = ""
_piper_key = ""


def load_config_file() -> None:
    global CFG
    merged = dict(DEFAULTS)
    if CONFIG_PATH.is_file():
        try:
            merged.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            print(f"Ignoring invalid {CONFIG_PATH}")
    CFG = merged


def save_config_file() -> None:
    CONFIG_PATH.write_text(json.dumps(CFG, indent=2) + "\n", encoding="utf-8")


def public_config() -> dict[str, Any]:
    data = dict(CFG)
    data["wifi_pass_set"] = bool(data.get("wifi_pass"))
    data["wifi_pass"] = ""
    data["sample_rate"] = TARGET_RATE
    return data


load_config_file()

app = FastAPI(title="ESP32 AI Buddy", version="1.1.0")


def check_token(authorization: str | None, write: bool = False) -> None:
    token = str(CFG.get("api_token") or "").strip()
    if not token:
        return
    if write or True:
        expected = f"Bearer {token}"
        if authorization != expected:
            # Health and the web page stay open. Config writes and AI calls need the token.
            if write:
                raise HTTPException(status_code=401, detail="Invalid or missing token")


def require_token(authorization: str | None) -> None:
    token = str(CFG.get("api_token") or "").strip()
    if not token:
        return
    if authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="Invalid or missing token")


def pick_whisper_device() -> tuple[str, str]:
    requested = str(CFG.get("whisper_device") or "auto").lower()
    if requested == "cpu":
        return "cpu", "int8"
    if requested == "cuda":
        return "cuda", "float16"
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


def whisper_model() -> WhisperModel:
    global _whisper, _whisper_key
    name = str(CFG["whisper_model"])
    device, compute = pick_whisper_device()
    key = f"{name}:{device}:{compute}"
    with LOCK:
        if _whisper is None or _whisper_key != key:
            print(f"Loading Whisper {name} on {device}/{compute}")
            _whisper = WhisperModel(name, device=device, compute_type=compute)
            _whisper_key = key
        return _whisper


def find_piper_files() -> tuple[Path, Path]:
    folder = Path(str(CFG["piper_voices_dir"]))
    voice = str(CFG["piper_voice"])
    onnx = folder / f"{voice}.onnx"
    config = folder / f"{voice}.onnx.json"
    if not onnx.is_file() or not config.is_file():
        raise FileNotFoundError(f"Piper voice missing: {onnx} and {config}")
    return onnx, config


def piper_voice() -> PiperVoice:
    global _piper, _piper_key
    onnx, config = find_piper_files()
    key = str(onnx)
    with LOCK:
        if _piper is None or _piper_key != key:
            print(f"Loading Piper voice {onnx.name}")
            _piper = PiperVoice.load(str(onnx), config_path=str(config), use_cuda=False)
            _piper_key = key
        return _piper


def pcm16_to_float(pcm: bytes, sample_rate: int) -> tuple[np.ndarray, int]:
    if len(pcm) < 2:
        return np.zeros(0, dtype=np.float32), TARGET_RATE
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    if sample_rate <= 0:
        sample_rate = TARGET_RATE
    if sample_rate != TARGET_RATE and samples.size:
        duration = samples.size / float(sample_rate)
        new_len = max(1, int(duration * TARGET_RATE))
        x_old = np.linspace(0.0, 1.0, samples.size, endpoint=False)
        x_new = np.linspace(0.0, 1.0, new_len, endpoint=False)
        samples = np.interp(x_new, x_old, samples).astype(np.float32)
        sample_rate = TARGET_RATE
    return samples, sample_rate


def transcribe_pcm(pcm: bytes, sample_rate: int) -> str:
    audio, _ = pcm16_to_float(pcm, sample_rate)
    if audio.size < TARGET_RATE // 10:
        return ""
    segments, _info = whisper_model().transcribe(
        audio,
        language="en",
        beam_size=1,
        vad_filter=True,
        without_timestamps=True,
    )
    return " ".join(seg.text.strip() for seg in segments).strip()


async def ollama_chat(user_text: str, history: list[dict[str, str]] | None = None) -> str:
    messages: list[dict[str, str]] = [{"role": "system", "content": str(CFG["system_prompt"])}]
    max_history = int(CFG["max_history"])
    if history:
        messages.extend(history[-max_history:])
    messages.append({"role": "user", "content": user_text})
    payload = {
        "model": CFG["ollama_model"],
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.7, "num_predict": 160},
    }
    url = str(CFG["ollama_url"]).rstrip("/")
    async with httpx.AsyncClient(timeout=180.0) as client:
        try:
            response = await client.post(f"{url}/api/chat", json=payload)
        except httpx.ConnectError as exc:
            raise HTTPException(status_code=503, detail=f"Ollama is not reachable: {exc}") from exc
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Ollama error: {response.text[:300]}")
    data = response.json()
    reply = (data.get("message") or {}).get("content") or ""
    reply = " ".join(reply.split()).strip()
    limit = int(CFG["max_reply_chars"])
    if len(reply) > limit:
        reply = reply[: limit - 1].rsplit(" ", 1)[0] + "…"
    return reply or "I did not catch that."


def synthesize_pcm(text: str) -> tuple[bytes, int]:
    voice = piper_voice()
    cfg = getattr(voice, "config", None)
    native_rate = int(getattr(cfg, "sample_rate", 22050)) if cfg else 22050
    audio: np.ndarray
    rate = native_rate
    if hasattr(voice, "synthesize_wav"):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            voice.synthesize_wav(text, wav_file)
        buffer.seek(0)
        audio, rate = sf.read(buffer, dtype="int16")
    else:
        frames: list[bytes] = []
        for chunk in voice.synthesize(text):
            raw = getattr(chunk, "audio_int16_bytes", None)
            if raw:
                frames.append(raw)
            elif getattr(chunk, "audio_int16_array", None) is not None:
                frames.append(np.asarray(chunk.audio_int16_array, dtype=np.int16).tobytes())
        audio = np.frombuffer(b"".join(frames), dtype="<i2")
        rate = native_rate
    if audio.ndim > 1:
        audio = audio[:, 0]
    if rate != TARGET_RATE and audio.size:
        duration = audio.size / float(rate)
        new_len = max(1, int(duration * TARGET_RATE))
        x_old = np.linspace(0.0, 1.0, audio.size, endpoint=False)
        x_new = np.linspace(0.0, 1.0, new_len, endpoint=False)
        audio = np.interp(x_new, x_old, audio.astype(np.float32)).astype(np.int16)
        rate = TARGET_RATE
    return np.asarray(audio, dtype="<i2").tobytes(), rate


def pcm_response(pcm: bytes, sample_rate: int, extra: dict[str, str] | None = None) -> Response:
    headers = {
        "X-Sample-Rate": str(sample_rate),
        "X-Channels": "1",
        "X-Encoding": "s16le",
        "Content-Length": str(len(pcm)),
    }
    if extra:
        headers.update(extra)
    return Response(content=pcm, media_type="audio/l16", headers=headers)


def ascii_header(value: str, limit: int = 180) -> str:
    cleaned = "".join(ch if 32 <= ord(ch) < 127 else " " for ch in value)
    return cleaned[:limit]


def apply_update(incoming: dict[str, Any]) -> None:
    global CFG, _whisper, _piper, _whisper_key, _piper_key
    cleaned = {k: v for k, v in incoming.items() if k in DEFAULTS and v is not None}
    if not str(cleaned.get("wifi_pass") or ""):
        cleaned.pop("wifi_pass", None)
    int_keys = {
        "port",
        "server_port",
        "volume",
        "brightness",
        "max_record_s",
        "max_history",
        "max_reply_chars",
    }
    for key in int_keys:
        if key in cleaned:
            cleaned[key] = int(cleaned[key])
    with LOCK:
        old_whisper = CFG.get("whisper_model"), CFG.get("whisper_device")
        old_piper = CFG.get("piper_voice"), CFG.get("piper_voices_dir")
        CFG.update(cleaned)
        save_config_file()
        if (CFG.get("whisper_model"), CFG.get("whisper_device")) != old_whisper:
            _whisper = None
            _whisper_key = ""
        if (CFG.get("piper_voice"), CFG.get("piper_voices_dir")) != old_piper:
            _piper = None
            _piper_key = ""


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health() -> dict[str, Any]:
    ollama_ok = False
    ollama_error = ""
    url = str(CFG["ollama_url"]).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            ping = await client.get(f"{url}/api/tags")
            ollama_ok = ping.status_code == 200
    except Exception as exc:
        ollama_error = str(exc)
    piper_ok = True
    try:
        find_piper_files()
    except FileNotFoundError:
        piper_ok = False
    return {
        "ok": ollama_ok and piper_ok,
        "ollama": ollama_ok,
        "ollama_error": ollama_error,
        "whisper_model": CFG["whisper_model"],
        "ollama_model": CFG["ollama_model"],
        "piper_voice": CFG["piper_voice"],
        "piper": piper_ok,
        "sample_rate": TARGET_RATE,
        "max_record_s": CFG["max_record_s"],
        "web_ui": "/",
    }


@app.get("/api/models")
async def models() -> dict[str, Any]:
    url = str(CFG["ollama_url"]).rstrip("/")
    names: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            ping = await client.get(f"{url}/api/tags")
            if ping.status_code == 200:
                names = [m.get("name") for m in ping.json().get("models", []) if m.get("name")]
    except Exception:
        pass
    return {"models": names}


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    return public_config()


@app.put("/api/config")
async def put_config(
    payload: dict[str, Any],
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_token(authorization) if str(CFG.get("api_token") or "") else None
    token = str(CFG.get("api_token") or "").strip()
    if token:
        require_token(authorization)
    apply_update(payload)
    return public_config()


@app.get("/api/config/sdcard")
async def download_sd_config() -> Response:
    data = dict(CFG)
    data.pop("host", None)
    data.pop("piper_voices_dir", None)
    body = json.dumps(data, indent=2) + "\n"
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="buddy.json"'},
    )


@app.post("/api/stt")
async def stt(
    request: Request,
    authorization: str | None = Header(default=None),
    x_sample_rate: str | None = Header(default="16000"),
) -> JSONResponse:
    require_token(authorization)
    pcm = await request.body()
    try:
        rate = int(x_sample_rate or "16000")
    except ValueError:
        rate = 16000
    text = transcribe_pcm(pcm, rate)
    return JSONResponse({"text": text})


@app.post("/api/chat")
async def chat(
    payload: dict[str, Any],
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    require_token(authorization)
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Missing text")
    history = payload.get("history") or []
    if not isinstance(history, list):
        history = []
    clean_history = []
    for item in history:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = str(item.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            clean_history.append({"role": role, "content": content})
    reply = await ollama_chat(text, clean_history)
    return JSONResponse({"reply": reply})


@app.post("/api/tts")
async def tts(
    payload: dict[str, Any],
    authorization: str | None = Header(default=None),
):
    require_token(authorization)
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Missing text")
    pcm, rate = synthesize_pcm(text)
    return pcm_response(pcm, rate)


@app.post("/api/talk")
async def talk(
    request: Request,
    authorization: str | None = Header(default=None),
    x_sample_rate: str | None = Header(default="16000"),
):
    require_token(authorization)
    pcm_in = await request.body()
    try:
        rate = int(x_sample_rate or "16000")
    except ValueError:
        rate = 16000
    transcript = transcribe_pcm(pcm_in, rate)
    if not transcript:
        reply = "I did not hear anything. Try again a little closer."
    else:
        reply = await ollama_chat(transcript)
    pcm_out, out_rate = synthesize_pcm(reply)
    return pcm_response(
        pcm_out,
        out_rate,
        extra={
            "X-Transcript": ascii_header(transcript or "(silence)"),
            "X-Reply": ascii_header(reply),
        },
    )


def main() -> None:
    import uvicorn

    host = str(CFG.get("host") or "0.0.0.0")
    port = int(CFG.get("port") or 8080)
    print(f"AI Buddy listening on {host}:{port}")
    uvicorn.run("ai_buddy_server:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
