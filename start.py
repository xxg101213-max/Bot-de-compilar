"""Start TDLib Bot API and the Telegram compiler in one Railway container.

This is required for local-mode getFile paths to be visible to the bot.
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import requests

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
API_ID = os.getenv("TELEGRAM_API_ID", "").strip()
API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()
PORT = 8081


def migrate_cloud_once() -> None:
    """Opt-in one-time logout from cloud Telegram API before using local mode."""
    if os.getenv("TELEGRAM_CLOUD_LOGOUT", "0") != "1":
        return
    print("Migrando bot da API cloud para a API local (logOut)...", flush=True)
    response = requests.post(f"https://api.telegram.org/bot{TOKEN}/logOut", timeout=30)
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Falha na resposta do logOut: HTTP {response.status_code}") from exc
    if not payload.get("ok") or payload.get("result") is not True:
        # Do not expose token or other Telegram-provided fields in logs.
        raise RuntimeError(f"Nao foi possivel fazer logOut da API cloud (HTTP {response.status_code}).")
    print("logOut realizado. A API cloud fica indisponivel por ate 10 minutos.", flush=True)


def wait_for_local_api(process: subprocess.Popen, timeout: float = 120) -> None:
    url = f"http://127.0.0.1:{PORT}/bot{TOKEN}/getMe"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Servidor Bot API local terminou com codigo {process.returncode}.")
        try:
            response = requests.get(url, timeout=3)
            if response.ok and response.json().get("ok"):
                return
        except (requests.RequestException, ValueError):
            pass
        time.sleep(2)
    raise RuntimeError("Servidor Bot API local nao respondeu em 120 segundos.")


def run() -> None:
    if not TOKEN or not API_ID.isdecimal() or not API_HASH:
        raise RuntimeError("Configure TELEGRAM_BOT_TOKEN, TELEGRAM_API_ID e TELEGRAM_API_HASH na Railway.")
    if os.getenv("TELEGRAM_LOCAL_API_URL", "http://127.0.0.1:8081").rstrip("/") != f"http://127.0.0.1:{PORT}":
        raise RuntimeError("TELEGRAM_LOCAL_API_URL deve apontar para http://127.0.0.1:8081 neste container.")

    for folder in (Path("/app/telegram-data"), Path("/app/telegram-temp")):
        folder.mkdir(parents=True, exist_ok=True)

    migrate_cloud_once()
    server = subprocess.Popen([
        "telegram-bot-api",
        "--api-id", API_ID,
        "--api-hash", API_HASH,
        "--http-ip-address", "127.0.0.1",
        "--http-port", str(PORT),
        "--local",
        "--dir", "/app/telegram-data",
        "--temp-dir", "/app/telegram-temp",
    ])
    bot = None
    stopping = False

    def stop(signum: int, _frame) -> None:
        nonlocal stopping
        stopping = True
        print(f"Encerrando (sinal {signum})...", flush=True)
        if bot and bot.poll() is None:
            bot.terminate()
        if server.poll() is None:
            server.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        wait_for_local_api(server)
        if stopping:
            return
        print("API local pronta. Iniciando bot Python.", flush=True)
        bot = subprocess.Popen([sys.executable, "-u", "bot.py"])
        while not stopping:
            if bot.poll() is not None:
                raise RuntimeError(f"Bot Python terminou (codigo {bot.returncode}).")
            if server.poll() is not None:
                raise RuntimeError(f"Servidor local terminou (codigo {server.returncode}).")
            time.sleep(1)
    finally:
        for process in (bot, server):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"Erro ao iniciar: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
