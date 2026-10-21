"""Start the Telegram Local Bot API and the compiler bot on one Railway service.

Provides actionable, sanitized diagnostics when the local API does not become ready.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

import requests

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
API_ID = os.getenv("TELEGRAM_API_ID", "").strip()
API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()
PORT = 8081
LOCAL_URL = f"http://127.0.0.1:{PORT}"


def safe(message: str) -> str:
    """Keep credentials out of exceptions and Telegram error descriptions."""
    for secret in (TOKEN, API_HASH):
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return message[:350]


def validate_environment() -> None:
    missing = [name for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_API_ID", "TELEGRAM_API_HASH")
               if not os.getenv(name, "").strip()]
    if missing:
        raise RuntimeError("Variaveis ausentes: " + ", ".join(missing))
    if not API_ID.isdecimal():
        raise RuntimeError("TELEGRAM_API_ID deve conter apenas numeros.")
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", TOKEN):
        raise RuntimeError("TELEGRAM_BOT_TOKEN tem formato invalido; confira no BotFather.")
    configured = os.getenv("TELEGRAM_LOCAL_API_URL", LOCAL_URL).rstrip("/")
    if configured != LOCAL_URL:
        raise RuntimeError(f"TELEGRAM_LOCAL_API_URL deve ser {LOCAL_URL}.")


def migrate_cloud_once() -> None:
    if os.getenv("TELEGRAM_CLOUD_LOGOUT", "0").strip() != "1":
        print("TELEGRAM_CLOUD_LOGOUT=0: nao executar logout da API em nuvem.", flush=True)
        return
    print("Executando logOut uma vez para migrar o bot da API Telegram em nuvem...", flush=True)
    try:
        response = requests.post(f"https://api.telegram.org/bot{TOKEN}/logOut", timeout=30)
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError("Falha de rede ao solicitar logOut na API em nuvem: " + safe(str(exc))) from None
    if not response.ok or payload.get("ok") is not True or payload.get("result") is not True:
        description = safe(str(payload.get("description", "sem detalhes")))
        raise RuntimeError(f"logOut nao confirmado (HTTP {response.status_code}): {description}")
    print("logOut confirmado. Depois da primeira inicializacao, volte TELEGRAM_CLOUD_LOGOUT para 0.", flush=True)


def check_local_api_once() -> tuple[bool, str]:
    """Check local API; return detailed sanitized failure (not just a silent timeout)."""
    try:
        r = requests.get(f"{LOCAL_URL}/bot{TOKEN}/getMe", timeout=(3, 12))
        try:
            payload = r.json()
        except ValueError:
            return False, f"HTTP {r.status_code}, resposta nao-JSON: {safe(r.text)}"
        if r.ok and payload.get("ok") and payload.get("result", {}).get("is_bot"):
            return True, "getMe respondeu com sucesso"
        description = safe(str(payload.get("description", "resposta sem detalhes")))
        return False, f"HTTP {r.status_code}; erro={payload.get('error_code', 'sem codigo')}; {description}"
    except requests.exceptions.ConnectionError:
        return False, "Conexao recusada ao acessar 127.0.0.1:8081 (servidor nao abriu a porta)."
    except requests.exceptions.Timeout:
        return False, "getMe na API local demorou mais de 12 segundos (possivel rede/TDLib bloqueada)."
    except requests.RequestException as exc:
        return False, safe(type(exc).__name__ + ": " + str(exc))


def wait_for_local_api(server: subprocess.Popen, timeout_seconds: float = 150.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_failure = "Ainda nao testado"
    attempt = 0
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError(f"telegram-bot-api terminou antes de ficar pronto (exit={server.returncode}).")
        attempt += 1
        ready, detail = check_local_api_once()
        if ready:
            print("API local pronta. Iniciando bot Python.", flush=True)
            return
        last_failure = detail
        # Keep logs informative but avoid needless flooding or secret leakage.
        if attempt <= 3 or attempt % 3 == 0:
            print(f"Tentativa {attempt}: API local ainda nao pronta: {detail}", flush=True)
        time.sleep(3)
    raise RuntimeError("Servidor local nao respondeu a getMe no prazo. Ultimo diagnostico: " + last_failure)


def run() -> None:
    validate_environment()
    for folder in (Path("/app/telegram-data"), Path("/app/telegram-temp")):
        folder.mkdir(parents=True, exist_ok=True)
    print("Iniciando bot Telegram com servidor API local (sem divulgar credenciais).", flush=True)
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
    bot: subprocess.Popen | None = None
    stopping = False

    def stop(signum: int, _frame) -> None:
        nonlocal stopping
        stopping = True
        print(f"Recebido sinal {signum}; encerrando com seguranca.", flush=True)
        if bot is not None and bot.poll() is None:
            bot.terminate()
        if server.poll() is None:
            server.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        wait_for_local_api(server)
        if stopping:
            return
        bot = subprocess.Popen([sys.executable, "-u", "bot.py"])
        while not stopping:
            if bot.poll() is not None:
                raise RuntimeError(f"bot.py terminou (exit={bot.returncode}). Consulte traceback acima.")
            if server.poll() is not None:
                raise RuntimeError(f"telegram-bot-api terminou (exit={server.returncode}).")
            time.sleep(1)
    finally:
        for child in (bot, server):
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print("Erro ao iniciar: " + safe(str(exc)), file=sys.stderr, flush=True)
        sys.exit(1)
