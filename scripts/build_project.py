#!/usr/bin/env python3
"""Trusted CI-side build script. Input is untrusted and is only opened in an ephemeral runner."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import zipfile

MAX_FILES = 2000
MAX_UNCOMPRESSED = 400 * 1024 * 1024


def safe_unpack(archive_path: Path, dest: Path) -> None:
    dest = dest.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_FILES or sum(i.file_size for i in infos) > MAX_UNCOMPRESSED:
            raise ValueError("ZIP grande demais.")
        for info in infos:
            # Reject traversal, special Unix files, Windows drive prefixes, and symlinks.
            name = info.filename.replace("\\", "/")
            parts = Path(name).parts
            if (not name or name.startswith("/") or ".." in parts
                    or (len(parts) and ":" in parts[0])):
                raise ValueError(f"Caminho inseguro no ZIP: {name}")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError(f"Link simbolico nao permitido: {name}")
            out = (dest / name).resolve()
            if not out.is_relative_to(dest):
                raise ValueError(f"Caminho fora do diretorio: {name}")
            if info.is_dir():
                out.mkdir(parents=True, exist_ok=True)
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, out.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def find_project(root: Path, pattern: str) -> Path | None:
    found = sorted(root.rglob(pattern), key=lambda p: (len(p.parts), str(p)))
    # Skip hidden build/system folders accidentally included in uploaded archives.
    found = [p for p in found if not any(part in {".git", ".build", "DerivedData", "node_modules"}
                                           for part in p.relative_to(root).parts)]
    return found[0] if found else None


def run(args: list[str], cwd: Path, extra_env: dict[str, str] | None = None) -> None:
    print("EXEC:", " ".join(args), "cwd=", cwd, flush=True)
    env = {**os.environ, **(extra_env or {})}
    subprocess.run(args, cwd=cwd, check=True, env=env)


def compile_dylib(root: Path, output: Path) -> None:
    candidates = [p for p in root.rglob("Makefile")
                  if p.is_file() and "THEOS" in p.read_text(encoding="utf-8", errors="ignore")]
    if not candidates:
        raise ValueError("Nao encontrei Makefile de projeto Theos dentro do ZIP.")
    makefile = min(candidates, key=lambda p: len(p.parts))
    directory = makefile.parent
    env = {"THEOS": str(Path.home() / "theos")}
    if not (Path(env["THEOS"]) / "makefiles").exists():
        raise RuntimeError("Theos nao foi instalado no runner.")
    run(["make", "clean"], cwd=directory, extra_env=env)
    run(["make", "-j2"], cwd=directory, extra_env=env)
    dylibs = sorted(directory.rglob("*.dylib"), key=lambda p: p.stat().st_mtime, reverse=True)
    # Only take outputs in .theos/obj, never a pre-supplied binary from ZIP.
    dylibs = [p for p in dylibs if ".theos" in p.relative_to(directory).parts
              and "obj" in p.relative_to(directory).parts]
    if not dylibs:
        raise ValueError("Make executou, mas nao gerou .dylib. Verifique se e um projeto tweak.")
    for file in dylibs[:10]:
        shutil.copy2(file, output / file.name)
    print("Arquivos produzidos:", [p.name for p in output.iterdir()], flush=True)


def get_schemes(container: Path, kind: str) -> list[str]:
    cmd = ["xcodebuild", "-list", "-json", f"-{kind}", str(container)]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    data = json.loads(result.stdout)
    if kind == "project":
        return data.get("project", {}).get("schemes", [])
    return data.get("workspace", {}).get("schemes", [])


def compile_ipa(root: Path, output: Path) -> None:
    workspace = find_project(root, "*.xcworkspace")
    if workspace and any(part.endswith(".xcodeproj") for part in workspace.parts):
        workspace = None  # Xcode's internal project.xcworkspace isn't an external workspace.
    project = find_project(root, "*.xcodeproj")
    if workspace:
        kind, path = "workspace", workspace
    elif project:
        kind, path = "project", project
    else:
        raise ValueError("Nao encontrei .xcodeproj nem .xcworkspace dentro do ZIP.")
    schemes = get_schemes(path, kind)
    schemes = [s for s in schemes if not s.lower().endswith(("tests", "uitests"))]
    if not schemes:
        raise ValueError("Nenhum scheme compartilhado foi encontrado no projeto Xcode.")
    chosen = schemes[0]
    print(f"Projeto: {path.name} | scheme: {chosen}", flush=True)
    derived = root.parent / "DerivedData"
    run(["xcodebuild", f"-{kind}", str(path), "-scheme", chosen,
         "-configuration", "Release", "-sdk", "iphoneos", "-destination", "generic/platform=iOS",
         "-derivedDataPath", str(derived), "CODE_SIGNING_ALLOWED=NO", "build"], cwd=path.parent)
    release_dir = derived / "Build" / "Products" / "Release-iphoneos"
    apps = sorted(release_dir.glob("*.app"))
    if not apps:
        raise ValueError("Xcode nao produziu um aplicativo iOS .app (talvez seja framework/library).")
    payload = root.parent / "Payload"
    payload.mkdir(exist_ok=True)
    shutil.copytree(apps[0], payload / apps[0].name, symlinks=False)
    ipa_path = output / f"{apps[0].stem}-unsigned.ipa"
    with zipfile.ZipFile(ipa_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=5) as zip_file:
        for file in payload.rglob("*"):
            if file.is_file():
                zip_file.write(file, file.relative_to(root.parent))
    print(f"IPA sem assinatura criado: {ipa_path}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", required=True, type=Path)
    parser.add_argument("--mode", choices=["auto", "dylib", "ipa"], required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    source = Path.cwd() / "build-source"
    source.mkdir()
    args.out.mkdir(parents=True, exist_ok=True)
    safe_unpack(args.zip, source)
    mode = args.mode
    if mode == "auto":
        mode = "ipa" if find_project(source, "*.xcodeproj") else "dylib"
    print(f"Compilando modo: {mode}", flush=True)
    if mode == "dylib":
        compile_dylib(source, args.out)
    else:
        compile_ipa(source, args.out)
    if not any(args.out.iterdir()):
        raise RuntimeError("Build terminou sem produzir arquivos.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
        print(f"ERRO DE COMPILACAO: {error}", file=sys.stderr, flush=True)
        sys.exit(1)
