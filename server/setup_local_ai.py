#!/usr/bin/env python3
"""Bootstrap the Ubuntu side of Cardputer Local AI.

Ubuntu must already be installed. Ollama/Whisper/Piper are configurable because
their installation and APIs differ between setups.
"""
from pathlib import Path
import json, os, subprocess, sys, shutil

APP = Path("/opt/cardputer-ai")
VENV = APP / ".venv"
PORT = 8765

APP_CODE = 'from fastapi import FastAPI, Request, HTTPException\nfrom fastapi.responses import Response\nfrom pydantic import BaseModel\nfrom pathlib import Path\nimport tempfile, subprocess, shlex, json, httpx\n\nAPP = Path("/opt/cardputer-ai")\nCFG = json.loads((APP / "config.json").read_text())\napp = FastAPI(title="Cardputer Local AI")\n\nclass ChatIn(BaseModel):\n    text: str\n    history: list[dict] = []\n\ndef ollama_chat(text, history):\n    messages = [{"role": "system", "content": CFG["ollama"]["system_prompt"]}]\n    messages += history[-12:]\n    messages.append({"role": "user", "content": text})\n    r = httpx.post(CFG["ollama"]["url"].rstrip("/") + "/api/chat",\n                   json={"model": CFG["ollama"]["model"], "messages": messages, "stream": False},\n                   timeout=180)\n    r.raise_for_status()\n    return r.json()["message"]["content"].strip()\n\ndef whisper_cli(path):\n    outdir = Path(tempfile.mkdtemp(prefix="whisper-"))\n    cmd = shlex.split(CFG["whisper"]["command"])\n    cmd += [str(path), "--output_format", "txt", "--output_dir", str(outdir)]\n    if CFG["whisper"].get("language"):\n        cmd += ["--language", CFG["whisper"]["language"]]\n    subprocess.run(cmd, check=True, timeout=240)\n    files = list(outdir.glob("*.txt"))\n    return files[0].read_text(errors="ignore").strip() if files else ""\n\ndef transcribe(path):\n    url = CFG["whisper"].get("url", "").strip()\n    if not url:\n        return whisper_cli(path)\n    with open(path, "rb") as f:\n        r = httpx.post(url, files={"file": (path.name, f, "audio/wav")},\n                       data={"model": "whisper", "language": CFG["whisper"].get("language","")},\n                       timeout=240)\n    r.raise_for_status()\n    return r.json().get("text", "").strip()\n\ndef piper_cli(text):\n    out = Path(tempfile.mktemp(prefix="piper-", suffix=".wav"))\n    cmd = shlex.split(CFG["piper"]["command"])\n    if CFG["piper"].get("model"):\n        cmd += ["--model", CFG["piper"]["model"]]\n    cmd += ["--output_file", str(out)]\n    subprocess.run(cmd, input=text.encode(), check=True, timeout=180)\n    data = out.read_bytes()\n    out.unlink(missing_ok=True)\n    return data\n\ndef synthesize(text):\n    url = CFG["piper"].get("url", "").strip()\n    if not url:\n        return piper_cli(text)\n    r = httpx.post(url, json={"text": text}, timeout=180)\n    r.raise_for_status()\n    return r.content\n\n@app.get("/api/health")\ndef health():\n    return {"ok": True, "ollama_model": CFG["ollama"]["model"]}\n\n@app.post("/api/chat")\ndef chat(body: ChatIn):\n    return {"text": ollama_chat(body.text.strip(), body.history)}\n\n@app.post("/api/voice")\nasync def voice(request: Request):\n    if not request.headers.get("content-type","").startswith("audio/"):\n        raise HTTPException(415, "Send raw audio/wav")\n    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as f:\n        path = Path(f.name)\n        async for chunk in request.stream():\n            f.write(chunk)\n    try:\n        transcript = transcribe(path)\n        if not transcript:\n            raise HTTPException(422, "Whisper returned no text")\n        answer = ollama_chat(transcript, [])\n        return {"transcript": transcript, "text": answer}\n    finally:\n        path.unlink(missing_ok=True)\n\n@app.post("/api/tts")\nasync def tts(body: ChatIn):\n    return Response(synthesize(body.text), media_type="audio/wav")\n'

def run(cmd, check=True):
    print("+", " ".join(cmd))
    return subprocess.run(cmd, check=check)

def ask(label, default):
    value = input(f"{label} [{default}]: ").strip()
    return value or default

def main():
    if os.geteuid() != 0:
        raise SystemExit("Run: sudo python3 setup_local_ai.py")

    print("=== Cardputer Local AI ===")
    model = ask("Ollama model", "qwen3:4b")
    whisper_url = ask("Whisper OpenAI-compatible URL (blank = CLI)", "")
    piper_url = ask("Piper HTTP URL (blank = CLI)", "")
    whisper_cmd = ask("Whisper CLI command", "whisper")
    piper_cmd = ask("Piper CLI command", "piper")
    piper_model = ask("Piper ONNX model path", "/opt/piper/en_US-lessac-medium.onnx")
    ts_hostname = ask("Tailscale server hostname", "cardputer-ai")

    APP.mkdir(parents=True, exist_ok=True)
    run(["apt-get", "update"])
    run(["apt-get", "install", "-y", "python3-venv", "python3-pip", "curl", "ca-certificates"])
    if shutil.which("ollama") is None:
        print("Installing Ollama...")
        run(["bash", "-lc", "curl -fsSL https://ollama.com/install.sh | sh"])
    run(["systemctl", "enable", "--now", "ollama"], check=False)
    run(["ollama", "pull", model])
    if not VENV.exists():
        run(["python3", "-m", "venv", str(VENV)])
    run([str(VENV/"bin/pip"), "install", "--upgrade", "pip"])
    run([str(VENV/"bin/pip"), "install", "fastapi", "uvicorn[standard]", "httpx"])

    config = {
        "listen": "0.0.0.0", "port": PORT,
        "ollama": {"url": "http://127.0.0.1:11434", "model": model,
                   "system_prompt": "You are a helpful, concise local voice assistant running on an M5Stack Cardputer ADV."},
        "whisper": {"url": whisper_url, "command": whisper_cmd, "language": "en"},
        "piper": {"url": piper_url, "command": piper_cmd, "model": piper_model}
    }
    (APP/"config.json").write_text(json.dumps(config, indent=2) + "\n")
    (APP/"app.py").write_text(APP_CODE)

    service = f"""[Unit]
Description=Cardputer Local AI bridge
After=network-online.target ollama.service tailscaled.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={APP}
ExecStart={VENV/'bin/uvicorn'} app:app --host 0.0.0.0 --port {PORT}
Restart=always
RestartSec=3
User=root
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
"""
    Path("/etc/systemd/system/cardputer-ai.service").write_text(service)
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", "cardputer-ai.service"])

    if shutil.which("tailscale") is None:
        print("Installing Tailscale...")
        run(["bash", "-lc", "curl -fsSL https://tailscale.com/install.sh | sh"])
    print("Starting Tailscale; authenticate in the URL if prompted.")
    run(["tailscale", "up", "--hostname", ts_hostname], check=False)

    print("\nDone.")
    print("Server Tailscale IPv4:", end=" ")
    subprocess.run(["tailscale", "ip", "-4"], check=False)
    print(f"API: http://<TAILSCALE-IP>:{PORT}")
    print("Edit /opt/cardputer-ai/config.json if your Whisper/Piper API differs.")
    print("Restart: sudo systemctl restart cardputer-ai")

if __name__ == "__main__":
    main()
