#!/usr/bin/env python3
"""Install Ollama, Whisper, Piper, and the AI Buddy gateway on Ubuntu."""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
VENV = ROOT / ".venv"
VOICES = ROOT / "voices"
ENV_FILE = ROOT / ".env"

PIPER_VOICE_FILES = {
    "en_US-lessac-medium": {
        "onnx": (
            "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
            "en/en_US/lessac/medium/en_US-lessac-medium.onnx"
        ),
        "json": (
            "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
            "en/en_US/lessac/medium/en_US-lessac-medium.onnx.json"
        ),
    }
}

APT_PACKAGES = [
    "python3",
    "python3-venv",
    "python3-pip",
    "python3-dev",
    "build-essential",
    "ffmpeg",
    "git",
    "curl",
    "wget",
    "ca-certificates",
    "pciutils",
    "sox",
    "libsndfile1",
    "libsndfile1-dev",
]


def run(cmd: list[str] | str, check: bool = True, shell: bool = False, env: dict | None = None) -> subprocess.CompletedProcess:
    print(f"\n$ {cmd if isinstance(cmd, str) else ' '.join(cmd)}")
    return subprocess.run(cmd, check=check, shell=shell, env=env)


def need_root_hint() -> None:
    if os.geteuid() != 0:
        print("Some steps use sudo. You may be prompted for your password.")


def is_ubuntu() -> bool:
    os_release = Path("/etc/os-release")
    if not os_release.is_file():
        return False
    text = os_release.read_text(encoding="utf-8").lower()
    return "ubuntu" in text or "debian" in text


def apt_install() -> None:
    run(["sudo", "apt-get", "update"])
    run(["sudo", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", *APT_PACKAGES])


def install_ollama() -> None:
    if shutil.which("ollama"):
        print("Ollama is already installed.")
        return
    print("Installing Ollama…")
    req = urllib.request.Request(
        "https://ollama.com/install.sh",
        headers={"User-Agent": "esp32-ai-buddy/1.0"},
    )
    script = urllib.request.urlopen(req).read()
    proc = subprocess.run(["sudo", "sh"], input=script, check=True)
    del proc


def service_exists(name: str) -> bool:
    result = subprocess.run(
        ["systemctl", "list-unit-files", f"{name}.service"],
        capture_output=True,
        text=True,
    )
    return f"{name}.service" in (result.stdout or "")


def enable_ollama() -> None:
    if not service_exists("ollama"):
        print("No ollama systemd unit yet; starting ollama serve in the background is left to the installer.")
        return
    run(["sudo", "systemctl", "enable", "--now", "ollama"], check=False)


def pull_model(model: str) -> None:
    print(f"Downloading Ollama model {model} (this can take a while)…")
    run(["ollama", "pull", model])


# piper-tts 1.2.0 needs piper-phonemize, which only ships wheels up to Python 3.11.
PY_OK = ((3, 9), (3, 11))
PY_TARGET = "3.11"


def venv_python_ok(py: Path) -> bool:
    try:
        out = subprocess.run(
            [str(py), "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
            capture_output=True, text=True, check=True,
        ).stdout.split()
        return PY_OK[0] <= (int(out[0]), int(out[1])) <= PY_OK[1]
    except Exception:
        return False


def find_uv() -> str:
    found = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    if Path(found).exists():
        return found
    print("Installing uv (to fetch Python %s)..." % PY_TARGET)
    run("curl -LsSf https://astral.sh/uv/install.sh | sh", shell=True)
    found = str(Path.home() / ".local" / "bin" / "uv")
    if not Path(found).exists():
        raise SystemExit("Could not install uv. Install Python 3.11 manually and re-run.")
    return found


def create_venv() -> Path:
    vpy = VENV / "bin" / "python"
    if VENV.exists() and not venv_python_ok(vpy):
        print("Existing .venv uses an unsupported Python; recreating it.")
        shutil.rmtree(VENV)
    if not VENV.exists():
        if PY_OK[0] <= sys.version_info[:2] <= PY_OK[1]:
            run([sys.executable, "-m", "venv", str(VENV)])
        else:
            print(f"System Python {sys.version_info[0]}.{sys.version_info[1]} is too new for piper-tts; using Python {PY_TARGET} via uv.")
            run([find_uv(), "venv", "--seed", "--python", PY_TARGET, str(VENV)])
    pip = VENV / "bin" / "pip"
    run([str(pip), "install", "--upgrade", "pip", "wheel"])
    run([str(pip), "install", "-r", str(ROOT / "requirements.txt")])
    return pip


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 1000:
        print(f"Already present: {dest.name}")
        return
    print(f"Downloading {dest.name}…")
    tmp = dest.with_suffix(dest.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "esp32-ai-buddy/1.0"})
    with urllib.request.urlopen(request) as response, open(tmp, "wb") as handle:
        shutil.copyfileobj(response, handle)
    tmp.replace(dest)


def download_piper_voice(voice: str) -> None:
    files = PIPER_VOICE_FILES.get(voice)
    if not files:
        raise SystemExit(f"Unknown Piper voice '{voice}'. Known: {', '.join(PIPER_VOICE_FILES)}")
    download(files["onnx"], VOICES / f"{voice}.onnx")
    download(files["json"], VOICES / f"{voice}.onnx.json")


def write_env(model: str, whisper: str, token: str, port: int, voice: str) -> None:
    if ENV_FILE.exists():
        print(f"{ENV_FILE} already exists; leaving it unchanged.")
        return
    ENV_FILE.write_text(
        dedent(
            f"""\
            HOST=0.0.0.0
            PORT={port}
            API_TOKEN={token}
            OLLAMA_URL=http://127.0.0.1:11434
            OLLAMA_MODEL={model}
            WHISPER_MODEL={whisper}
            WHISPER_DEVICE=auto
            PIPER_VOICE={voice}
            PIPER_VOICES_DIR={VOICES}
            SYSTEM_PROMPT=You are Buddy, a helpful local AI on a tiny M5Stack Cardputer ADV. Reply in 1-3 short spoken sentences. No markdown, no lists, no emoji unless asked.
            MAX_HISTORY=8
            MAX_REPLY_CHARS=400
            """
        ),
        encoding="utf-8",
    )
    print(f"Wrote {ENV_FILE}")


def write_run_script() -> None:
    script = ROOT / "run.sh"
    script.write_text(
        dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            cd "{ROOT}"
            source "{VENV}/bin/activate"
            exec python ai_buddy_server.py
            """
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


def install_systemd(port: int) -> None:
    user = os.environ.get("SUDO_USER") or os.environ.get("USER") or "ubuntu"
    unit = dedent(
        f"""\
        [Unit]
        Description=ESP32 AI Buddy (Ollama + Whisper + Piper)
        After=network-online.target ollama.service
        Wants=network-online.target

        [Service]
        Type=simple
        User={user}
        WorkingDirectory={ROOT}
        EnvironmentFile=-{ENV_FILE}
        ExecStart={VENV}/bin/python {ROOT}/ai_buddy_server.py
        Restart=on-failure
        RestartSec=5

        [Install]
        WantedBy=multi-user.target
        """
    )
    tmp = Path("/tmp/ai-buddy.service")
    tmp.write_text(unit, encoding="utf-8")
    run(["sudo", "cp", str(tmp), "/etc/systemd/system/ai-buddy.service"])
    run(["sudo", "systemctl", "daemon-reload"])
    run(["sudo", "systemctl", "enable", "--now", "ai-buddy"])
    print(f"Gateway service started. Open http://<server-ip>:{port}/health")


def warmup_whisper(whisper: str) -> None:
    python = VENV / "bin" / "python"
    code = (
        "from faster_whisper import WhisperModel\n"
        f"print('Warming Whisper {whisper}…')\n"
        f"WhisperModel('{whisper}', device='cpu', compute_type='int8')\n"
        "print('Whisper ready')\n"
    )
    run([str(python), "-c", code], check=False)


def detect_lan_hint() -> str:
    try:
        out = subprocess.check_output(["hostname", "-I"], text=True).strip()
        return out.split()[0] if out else "YOUR.SERVER.IP"
    except Exception:
        return "YOUR.SERVER.IP"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Set up the local AI Buddy server on Ubuntu.")
    parser.add_argument("--model", default="llama3.2:3b", help="Ollama model name")
    parser.add_argument("--whisper", default="base.en", help="faster-whisper model")
    parser.add_argument("--voice", default="en_US-lessac-medium", help="Piper voice id")
    parser.add_argument("--token", default="", help="Optional Bearer token for the Cardputer")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--skip-apt", action="store_true")
    parser.add_argument("--skip-ollama", action="store_true")
    parser.add_argument("--skip-systemd", action="store_true", help="Do not install a systemd service")
    parser.add_argument("--run", action="store_true", help="Start the gateway in the foreground after setup")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not is_ubuntu():
        print("Warning: this script is meant for Ubuntu Server. Continuing anyway.")
    need_root_hint()

    if not args.skip_apt:
        apt_install()
    if not args.skip_ollama:
        install_ollama()
        enable_ollama()
        pull_model(args.model)

    create_venv()
    download_piper_voice(args.voice)
    write_env(args.model, args.whisper, args.token, args.port, args.voice)
    write_run_script()
    warmup_whisper(args.whisper)

    if not args.skip_systemd:
        install_systemd(args.port)

    ip = detect_lan_hint()
    print(
        dedent(
            f"""

            Setup complete.

            Health check:
              curl http://{ip}:{args.port}/health

            Put this on the Cardputer (firmware/include/secrets.h):
              WIFI_SSID / WIFI_PASS of your LAN
              SERVER_HOST {ip}
              SERVER_PORT {args.port}

            Foreground run (if you skipped systemd):
              {ROOT}/run.sh
            """
        )
    )
    if args.run:
        os.execv(str(VENV / "bin" / "python"), [str(VENV / "bin" / "python"), str(ROOT / "ai_buddy_server.py")])


if __name__ == "__main__":
    main()
