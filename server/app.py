from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from pathlib import Path
import tempfile, subprocess, shlex, json, httpx

APP = Path("/opt/cardputer-ai")
CFG = json.loads((APP / "config.json").read_text())
app = FastAPI(title="Cardputer Local AI")

class ChatIn(BaseModel):
    text: str
    history: list[dict] = []

def ollama_chat(text, history):
    messages = [{"role": "system", "content": CFG["ollama"]["system_prompt"]}]
    messages += history[-12:]
    messages.append({"role": "user", "content": text})
    r = httpx.post(CFG["ollama"]["url"].rstrip("/") + "/api/chat",
                   json={"model": CFG["ollama"]["model"], "messages": messages, "stream": False},
                   timeout=180)
    r.raise_for_status()
    return r.json()["message"]["content"].strip()

def whisper_cli(path):
    outdir = Path(tempfile.mkdtemp(prefix="whisper-"))
    cmd = shlex.split(CFG["whisper"]["command"])
    cmd += [str(path), "--output_format", "txt", "--output_dir", str(outdir)]
    if CFG["whisper"].get("language"):
        cmd += ["--language", CFG["whisper"]["language"]]
    subprocess.run(cmd, check=True, timeout=240)
    files = list(outdir.glob("*.txt"))
    return files[0].read_text(errors="ignore").strip() if files else ""

def transcribe(path):
    url = CFG["whisper"].get("url", "").strip()
    if not url:
        return whisper_cli(path)
    with open(path, "rb") as f:
        r = httpx.post(url, files={"file": (path.name, f, "audio/wav")},
                       data={"model": "whisper", "language": CFG["whisper"].get("language","")},
                       timeout=240)
    r.raise_for_status()
    return r.json().get("text", "").strip()

def piper_cli(text):
    out = Path(tempfile.mktemp(prefix="piper-", suffix=".wav"))
    cmd = shlex.split(CFG["piper"]["command"])
    if CFG["piper"].get("model"):
        cmd += ["--model", CFG["piper"]["model"]]
    cmd += ["--output_file", str(out)]
    subprocess.run(cmd, input=text.encode(), check=True, timeout=180)
    data = out.read_bytes()
    out.unlink(missing_ok=True)
    return data

def synthesize(text):
    url = CFG["piper"].get("url", "").strip()
    if not url:
        return piper_cli(text)
    r = httpx.post(url, json={"text": text}, timeout=180)
    r.raise_for_status()
    return r.content

@app.get("/api/health")
def health():
    return {"ok": True, "ollama_model": CFG["ollama"]["model"]}

@app.post("/api/chat")
def chat(body: ChatIn):
    return {"text": ollama_chat(body.text.strip(), body.history)}

@app.post("/api/voice")
async def voice(request: Request):
    if not request.headers.get("content-type","").startswith("audio/"):
        raise HTTPException(415, "Send raw audio/wav")
    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as f:
        path = Path(f.name)
        async for chunk in request.stream():
            f.write(chunk)
    try:
        transcript = transcribe(path)
        if not transcript:
            raise HTTPException(422, "Whisper returned no text")
        answer = ollama_chat(transcript, [])
        return {"transcript": transcript, "text": answer}
    finally:
        path.unlink(missing_ok=True)

@app.post("/api/tts")
async def tts(body: ChatIn):
    return Response(synthesize(body.text), media_type="audio/wav")
