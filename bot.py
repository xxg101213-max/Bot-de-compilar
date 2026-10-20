"""Telegram local Bot API -> temporary private GitHub release -> macOS Actions -> Telegram.

This bot never executes user source code in its own process/container.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import re
import tempfile
import time
from uuid import uuid4
import zipfile

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("compiler_bot")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GH_REPO = os.environ.get("GITHUB_REPO", "").strip()
GH_BRANCH = os.environ.get("GITHUB_BRANCH", "main").strip()
ALLOWED = {int(v.strip()) for v in os.environ.get("ALLOWED_USER_IDS", "").split(",") if v.strip().isdigit()}
MAX_UPLOAD = 100 * 1024 * 1024  # Local Telegram Bot API; also subject to GitHub upload limits.
MAX_RELEASE_UPLOAD = 100 * 1024 * 1024  # Larger source ZIP via private GitHub Release.
MAX_OUTPUT = 180 * 1024 * 1024  # Local Bot API supports sending larger generated files.
WAIT_SECONDS = 25 * 60
GITHUB_API = "https://api.github.com"


def validate_config() -> None:
    if not TOKEN or not GH_TOKEN or not ALLOWED:
        raise RuntimeError("Configure TELEGRAM_BOT_TOKEN, GITHUB_TOKEN e ALLOWED_USER_IDS.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", GH_REPO):
        raise RuntimeError("GITHUB_REPO precisa estar no formato dono/repositorio.")
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", GH_BRANCH):
        raise RuntimeError("GITHUB_BRANCH invalida.")


class Github:
    def __init__(self) -> None:
        self.base = f"{GITHUB_API}/repos/{GH_REPO}"
        self.headers = {
            "Authorization": f"Bearer {GH_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def request(self, method: str, path: str, **kwargs):
        r = requests.request(method, f"{self.base}{path}", headers=self.headers,
                             timeout=90, **kwargs)
        if not r.ok:
            message = r.text[:350].replace(GH_TOKEN, "[token]")
            raise RuntimeError(f"GitHub {method} {path}: HTTP {r.status_code}: {message}")
        return r.json() if r.content else None

    def check_uploaded_release(self, tag: str) -> None:
        """Allow only a source.zip uploaded by the repo owner via a published release."""
        release = self.request("GET", f"/releases/tags/{tag}")
        if release.get("draft"):
            raise ValueError("Publique a Release no GitHub antes de usar /release.")
        assets = [a for a in release.get("assets", []) if a.get("name") == "source.zip"]
        if len(assets) != 1:
            raise ValueError("A Release precisa conter exatamente um anexo chamado source.zip.")
        size = int(assets[0].get("size") or 0)
        if not 0 < size <= MAX_RELEASE_UPLOAD:
            raise ValueError("O source.zip da Release precisa ter entre 1 byte e 100 MB.")

    def upload_source(self, zip_path: Path, tag: str) -> int:
        release = self.request("POST", "/releases", json={
            "tag_name": tag, "target_commitish": GH_BRANCH, "name": tag,
            "draft": False, "prerelease": True,
        })
        upload_url = release["upload_url"].split("{", 1)[0]
        try:
            with zip_path.open("rb") as fp:
                r = requests.post(upload_url, params={"name": "source.zip"}, data=fp,
                                  headers={**self.headers, "Content-Type": "application/zip"},
                                  timeout=120)
            if r.status_code != 201:
                raise RuntimeError(f"GitHub upload do ZIP: HTTP {r.status_code}: {r.text[:300]}")
        except Exception:
            try:
                self.delete_release(int(release["id"]), tag)
            except Exception:
                pass
            raise
        return int(release["id"])

    def dispatch(self, tag: str, mode: str, job_id: str) -> None:
        self.request("POST", "/actions/workflows/build.yml/dispatches", json={
            "ref": GH_BRANCH,
            "inputs": {"release_tag": tag, "mode": mode, "job_id": job_id},
        })

    def find_run(self, job_id: str):
        data = self.request("GET", "/actions/workflows/build.yml/runs", params={
            "event": "workflow_dispatch", "per_page": 50,
        })
        expected = f"compiler-{job_id}"
        return next((run for run in data.get("workflow_runs", [])
                     if run.get("display_title") == expected), None)

    def get_run(self, run_id: int):
        return self.request("GET", f"/actions/runs/{run_id}")

    def get_artifact_id(self, run_id: int, job_id: str) -> int:
        data = self.request("GET", f"/actions/runs/{run_id}/artifacts", params={"per_page": 100})
        expected = f"compiled-{job_id}"
        for artifact in data.get("artifacts", []):
            if artifact.get("name") == expected and not artifact.get("expired"):
                return int(artifact["id"])
        raise RuntimeError("Compilacao terminou, mas o artefato nao foi encontrado.")

    def download_artifact(self, artifact_id: int, dest: Path) -> None:
        with requests.get(f"{self.base}/actions/artifacts/{artifact_id}/zip",
                          headers=self.headers, stream=True, timeout=120) as response:
            response.raise_for_status()
            length = 0
            with dest.open("wb") as fp:
                for part in response.iter_content(1024 * 1024):
                    if part:
                        length += len(part)
                        if length > 220 * 1024 * 1024:
                            raise RuntimeError("Artefato maior que o limite do bot.")
                        fp.write(part)

    def delete_release(self, release_id: int | None, tag: str) -> None:
        if release_id is not None:
            self.request("DELETE", f"/releases/{release_id}")
        self.request("DELETE", f"/git/refs/tags/{tag}")


def choose_output(artifact_zip: Path, folder: Path) -> Path:
    """Extract only an expected build artifact; otherwise forward artifact ZIP."""
    with zipfile.ZipFile(artifact_zip) as archive:
        names = [n for n in archive.namelist() if not n.endswith("/")]
        if len(names) == 1:
            name = names[0]
            suffix = Path(name).suffix.lower()
            if suffix in {".ipa", ".dylib", ".deb"}:
                info = archive.getinfo(name)
                if info.file_size <= MAX_OUTPUT:
                    out = folder / Path(name).name
                    with archive.open(name) as src, out.open("wb") as dest:
                        import shutil
                        shutil.copyfileobj(src, dest)
                    return out
    return artifact_zip


async def authorized(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else None
    if uid not in ALLOWED:
        if update.effective_message:
            await update.effective_message.reply_text("Acesso nao autorizado. Seu Telegram ID: " + str(uid))
        return False
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    await update.message.reply_text(
        "Compilador iOS online! Envie o source em .zip (ate 100 MB).\n\n"
        "Escolha /modo auto, /modo dylib ou /modo ipa.\n"
        "auto detecta projetos Theos (Makefile) ou Xcode (.xcodeproj/.xcworkspace).\n"
        "Envio direto: o bot cria e apaga a Release temporaria automaticamente.\n"
        "Use /status para consultar a compilacao.\n\n"
        "OBS: .ipa gerado sem assinatura; assinatura/instalacao sao etapas separadas."
    )


async def mode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    mode = context.args[0].lower() if context.args else ""
    if mode not in {"auto", "dylib", "ipa"}:
        await update.message.reply_text("Use /modo auto, /modo dylib ou /modo ipa")
        return
    context.user_data["mode"] = mode
    await update.message.reply_text(f"Modo selecionado: {mode}")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    active = context.application.bot_data.get("active", {})
    job = active.get(update.effective_user.id)
    await update.message.reply_text(f"Em andamento: {job}" if job else "Nenhuma compilacao em andamento.")


async def compile_from_release(update: Update, github: Github, *, tag: str, mode: str,
                               job_id: str, status) -> None:
    """Run the existing isolated workflow and return output in the Telegram chat."""
    with tempfile.TemporaryDirectory(prefix="tg_result_") as temp:
        folder = Path(temp)
        await asyncio.to_thread(github.dispatch, tag, mode, job_id)
        await status.edit_text(f"Compilando (modo {mode})... ID: {job_id}")
        run = None
        start_at = time.monotonic()
        while time.monotonic() - start_at < WAIT_SECONDS:
            await asyncio.sleep(12)
            if run is None:
                run = await asyncio.to_thread(github.find_run, job_id)
                if run is None:
                    continue
            state = await asyncio.to_thread(github.get_run, int(run["id"]))
            if state.get("status") != "completed":
                continue
            url = state.get("html_url", f"https://github.com/{GH_REPO}/actions")
            if state.get("conclusion") != "success":
                await status.edit_text(f"Falha na compilacao. Consulte o log:\n{url}")
                return
            artifact_id = await asyncio.to_thread(github.get_artifact_id, int(run["id"]), job_id)
            artifact = folder / "compilado.zip"
            await asyncio.to_thread(github.download_artifact, artifact_id, artifact)
            output = choose_output(artifact, folder)
            if output.stat().st_size > MAX_OUTPUT:
                await status.edit_text(f"Compilado, mas o arquivo excede {MAX_OUTPUT // (1024 * 1024)} MB. Baixe no GitHub:\n{url}")
                return
            # Local Bot API accepts a path directly and does not reupload big files from the bot process.
            await update.message.reply_document(
                document=output, filename=output.name,
                caption=f"Compilacao concluida! ID: {job_id}",
                read_timeout=180, write_timeout=180,
            )
            await status.edit_text(f"Compilacao concluida! ID: {job_id}")
            return
        await status.edit_text(f"Tempo maximo de espera atingido. Confira as Actions:\nhttps://github.com/{GH_REPO}/actions")


def reserve_job(context: ContextTypes.DEFAULT_TYPE, uid: int) -> str | None:
    active = context.application.bot_data.setdefault("active", {})
    if uid in active:
        return None
    job_id = uuid4().hex[:14]
    active[uid] = job_id
    return job_id


async def release_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Compile a privately uploaded large source ZIP without passing via Telegram getFile."""
    if not await authorized(update):
        return
    tag = context.args[0] if len(context.args) == 1 else ""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", tag):
        await update.message.reply_text(
            "Use /release NOME_DA_TAG. A Release precisa ter um anexo chamado source.zip "
            "(ate 100 MB) no mesmo repositorio privado do bot."
        )
        return
    uid = update.effective_user.id
    job_id = reserve_job(context, uid)
    if job_id is None:
        await update.message.reply_text("Voce ja tem uma compilacao em andamento. Use /status.")
        return
    github = Github()
    status = await update.message.reply_text("Verificando source.zip na Release privada do GitHub...")
    try:
        await asyncio.to_thread(github.check_uploaded_release, tag)
        mode = context.user_data.get("mode", "auto")
        await compile_from_release(update, github, tag=tag, mode=mode, job_id=job_id, status=status)
    except (RuntimeError, ValueError, requests.RequestException) as exc:
        log.exception("Job via Release %s falhou", job_id)
        await status.edit_text(f"Erro: {str(exc)[:700]}")
    except Exception:
        log.exception("Erro inesperado no job via Release %s", job_id)
        await status.edit_text("Erro inesperado. Confira os logs do bot no Railway.")
    finally:
        context.application.bot_data["active"].pop(uid, None)
    # Intentionally keep user-managed releases. Owner deletes them manually when done.


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    doc = update.message.document
    if not doc.file_name or not doc.file_name.lower().endswith(".zip"):
        await update.message.reply_text("Envie o projeto compactado no formato .zip.")
        return
    if not doc.file_size or doc.file_size > MAX_UPLOAD:
        await update.message.reply_text(
            "O arquivo excede o limite configurado de 100 MB por ZIP. "
            "Envie um projeto menor ou ajuste a infraestrutura para volumes maiores."
        )
        return
    uid = update.effective_user.id
    job_id = reserve_job(context, uid)
    if job_id is None:
        await update.message.reply_text("Voce ja tem uma compilacao em andamento. Use /status.")
        return
    mode = context.user_data.get("mode", "auto")
    tag = f"compiler-src-{job_id}"
    release_id = None
    github = Github()
    try:
        with tempfile.TemporaryDirectory(prefix="tg_compile_") as temp:
            folder = Path(temp)
            source = folder / "source.zip"
            status = await update.message.reply_text(f"Recebido! Job {job_id}: enviando source para compilacao...")
            tg_file = await doc.get_file()
            # In local mode, getFile resolves to a path on the SAME container's disk.
            # PTB copies it to our temporary path when custom_path is provided.
            await tg_file.download_to_drive(custom_path=source)
            if not source.is_file():
                raise RuntimeError("A API local nao disponibilizou o ZIP; verifique o armazenamento.")
            if source.stat().st_size > MAX_UPLOAD:
                raise ValueError("ZIP excede 100 MB.")
            if not zipfile.is_zipfile(source):
                raise ValueError("ZIP invalido ou corrompido.")
            # Check metadata before shipping. Unpacking occurs only in an isolated Actions runner.
            with zipfile.ZipFile(source) as archive:
                files = [info for info in archive.infolist() if not info.is_dir()]
                if len(files) > 2000 or sum(f.file_size for f in files) > 400 * 1024 * 1024:
                    raise ValueError("ZIP excede o limite de 2000 arquivos ou 400 MB descompactados.")
            release_id = await asyncio.to_thread(github.upload_source, source, tag)
            await compile_from_release(update, github, tag=tag, mode=mode, job_id=job_id, status=status)
    except (RuntimeError, ValueError, zipfile.BadZipFile, requests.RequestException) as exc:
        log.exception("Job %s falhou", job_id)
        await update.message.reply_text(f"Erro: {str(exc)[:700]}")
    except Exception:
        log.exception("Erro inesperado no job %s", job_id)
        await update.message.reply_text("Erro inesperado. Confira os logs do bot no Railway.")
    finally:
        context.application.bot_data["active"].pop(uid, None)
        if release_id is not None:
            try:
                await asyncio.to_thread(github.delete_release, release_id, tag)
            except Exception as exc:
                log.warning("Nao foi possivel excluir ZIP temporario %s: %s", tag, exc)


def main():
    validate_config()
    local_api = os.environ.get("TELEGRAM_LOCAL_API_URL", "http://127.0.0.1:8081").rstrip("/")
    application = (
        Application.builder()
        .token(TOKEN)
        .base_url(f"{local_api}/bot")
        .base_file_url(f"{local_api}/file/bot")
        .local_mode(True)
        .concurrent_updates(4)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("modo", mode_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("release", release_cmd))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
