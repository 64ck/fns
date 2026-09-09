"""Проверка и установка обновлений.

Работает в двух режимах:

* **исходники** — если рядом есть репозиторий git, сравнивается текущая ветка
  с origin и предлагается `git pull`;
* **собранная программа (exe)** — сверяется отметка сборки, вшитая в файл, с
  последним релизом на GitHub; при согласии пользователя новая версия
  скачивается и подменяет текущую (старая сохраняется рядом как .old).

Проверка никогда не мешает запуску: сеть может быть недоступна, репозиторий —
отсутствовать, права — не позволять запись. Любая такая ситуация означает
«обновлений нет», а не ошибку.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import config

DEFAULT_REPO = "64ck/fns"
RELEASE_ASSET = "fns-portal.exe"
TIMEOUT = 12
GIT_TIMEOUT = 60


@dataclass
class UpdateInfo:
    available: bool = False
    mode: str = "none"          # git | release | none
    message: str = ""
    detail: str = ""
    download_url: str = ""
    behind: int = 0

    def __bool__(self) -> bool:
        return self.available


# ------------------------------------------------------------- отметка сборки

def build_info() -> dict:
    """Сведения о сборке: коммит и дата (кладутся в файл при сборке exe)."""
    for candidate in (config.BUNDLE_DIR / "reference" / "build_info.json",
                      config.REFERENCE_DIR / "build_info.json"):
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return {}


def describe_version() -> str:
    info = build_info()
    if info.get("commit"):
        stamp = str(info.get("built_at") or info.get("date") or "")[:16].replace("T", " ")
        return f"{stamp} · {info['commit'][:7]}".strip(" ·")
    return "версия из исходников"


def repo_name() -> str:
    return os.environ.get("FNS_REPO") or build_info().get("repo") or DEFAULT_REPO


# --------------------------------------------------------------------- git

def _git(root: Path, *args: str, timeout: int = GIT_TIMEOUT) -> tuple[int, str]:
    try:
        done = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return done.returncode, (done.stdout or done.stderr).strip()
    except (OSError, subprocess.SubprocessError) as error:
        return 1, str(error)


def git_root(start: Path | None = None) -> Path | None:
    """Каталог репозитория, если программа запущена из исходников."""
    current = Path(start or config.ROOT).resolve()
    for path in (current, *current.parents):
        if (path / ".git").exists():
            return path
    return None


def check_git(root: Path) -> UpdateInfo:
    code, branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if code or not branch:
        return UpdateInfo(message="git недоступен")
    code, _ = _git(root, "fetch", "--quiet", "origin", branch)
    if code:
        return UpdateInfo(message="не удалось связаться с origin")
    code, counts = _git(root, "rev-list", "--left-right", "--count",
                        f"HEAD...origin/{branch}")
    if code or not counts:
        return UpdateInfo(message="нет ветки origin/" + branch)
    ahead, behind = (int(part) for part in counts.split())
    if behind == 0:
        return UpdateInfo(mode="git", message="установлена последняя версия")
    return UpdateInfo(
        available=True, mode="git", behind=behind,
        message=f"доступно обновление: {behind} новых коммит(ов) в origin/{branch}",
        detail=f"локальных коммитов сверх origin: {ahead}" if ahead else "",
    )


def apply_git(root: Path) -> tuple[bool, str]:
    code, output = _git(root, "pull", "--ff-only")
    if code:
        return False, f"git pull не выполнен: {output}"
    return True, output or "обновлено"


# ------------------------------------------------------------------ релизы

def _fetch_json(url: str) -> dict | None:
    request = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json",
                      "User-Agent": "fns-portal-updater"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def check_release(repo: str | None = None) -> UpdateInfo:
    """Сравнивает отметку сборки с последним релизом на GitHub.

    Сборка выкладывается «скользящим» релизом (тег latest), поэтому сравнение
    идёт по времени публикации файла с точностью до секунды, а совпадение
    коммита считается признаком того же самого выпуска.
    """
    repo = repo or repo_name()
    data = _fetch_json(f"https://api.github.com/repos/{repo}/releases/latest")
    if not data:
        return UpdateInfo(message="сервер обновлений недоступен")
    asset = next((item for item in data.get("assets", [])
                  if item.get("name") == RELEASE_ASSET), None)
    if not asset:
        return UpdateInfo(message="в релизе нет файла программы")

    local = build_info()
    marker = " ".join(str(data.get(key, "")) for key in ("name", "body", "target_commitish"))
    commit = str(local.get("commit", ""))
    if commit and commit[:12] in marker:
        return UpdateInfo(mode="release", message="установлена последняя версия")

    remote_stamp = str(asset.get("updated_at") or data.get("published_at") or "")
    local_stamp = str(local.get("built_at", ""))
    if local_stamp and remote_stamp <= local_stamp:
        return UpdateInfo(mode="release", message="установлена последняя версия")

    return UpdateInfo(
        available=True, mode="release",
        message=f"доступна новая версия от {remote_stamp[:16].replace('T', ' ')}",
        detail=str(data.get("name") or data.get("tag_name", "")),
        download_url=asset.get("browser_download_url", ""),
    )


def download_release(url: str, destination: Path) -> Path:
    request = urllib.request.Request(url, headers={"User-Agent": "fns-portal-updater"})
    temporary = destination.with_suffix(destination.suffix + ".part")
    with urllib.request.urlopen(request, timeout=TIMEOUT * 10) as response, \
            temporary.open("wb") as fh:
        shutil.copyfileobj(response, fh, length=1 << 20)
    if temporary.stat().st_size < 5_000_000 or temporary.read_bytes()[:2] != b"MZ":
        temporary.unlink(missing_ok=True)
        raise RuntimeError("скачанный файл повреждён — обновление отменено")
    temporary.replace(destination)
    return destination


def apply_release(url: str) -> tuple[bool, str]:
    """Скачивает новую версию и подменяет текущий файл программы.

    Windows не даёт перезаписать запущенный exe, но позволяет его переименовать,
    поэтому текущая версия сохраняется рядом как .old и удаляется при следующем
    запуске.
    """
    if not getattr(sys, "frozen", False):
        return False, "подмена файла возможна только для собранной программы"
    current = Path(sys.executable)
    new_file = current.with_name(current.stem + ".new" + current.suffix)
    try:
        download_release(url, new_file)
    except Exception as error:  # noqa: BLE001 — причин может быть много, важен текст
        return False, f"не удалось скачать обновление: {error}"
    backup = current.with_name(current.stem + ".old" + current.suffix)
    try:
        backup.unlink(missing_ok=True)
        current.rename(backup)
        new_file.rename(current)
    except OSError as error:
        return False, f"не удалось заменить файл программы: {error}"
    return True, f"обновление установлено, прежняя версия сохранена как {backup.name}"


def cleanup_old() -> None:
    """Удаляет резервную копию, оставшуюся от прошлого обновления."""
    if not getattr(sys, "frozen", False):
        return
    current = Path(sys.executable)
    backup = current.with_name(current.stem + ".old" + current.suffix)
    try:
        backup.unlink(missing_ok=True)
    except OSError:
        pass


# ----------------------------------------------------------------- фасад

def check(root: Path | None = None) -> UpdateInfo:
    """Единая проверка: git, если есть репозиторий, иначе релизы GitHub."""
    repo_dir = git_root(root)
    if repo_dir is not None:
        return check_git(repo_dir)
    return check_release()


def apply(info: UpdateInfo, root: Path | None = None) -> tuple[bool, str]:
    if info.mode == "git":
        repo_dir = git_root(root)
        if repo_dir is None:
            return False, "репозиторий не найден"
        return apply_git(repo_dir)
    if info.mode == "release" and info.download_url:
        return apply_release(info.download_url)
    return False, "нечего устанавливать"
