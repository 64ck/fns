"""Проверка логики обновления: сравнение версий и работа без сети."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from fnsportal import update


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    return done.stdout.strip()


@pytest.fixture()
def repos(tmp_path):
    """Удалённый репозиторий и клон, отставший от него на один коммит."""
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(origin)], check=True)
    for key, value in (("user.email", "t@example.com"), ("user.name", "Тест")):
        _git(origin, "config", key, value)
    (origin / "readme.txt").write_text("1", encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "первый")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    for key, value in (("user.email", "t@example.com"), ("user.name", "Тест")):
        _git(clone, "config", key, value)
    return origin, clone


def test_git_check_reports_up_to_date(repos):
    _origin, clone = repos
    info = update.check_git(clone)
    assert not info.available
    assert info.mode == "git"


def test_git_check_and_pull(repos):
    origin, clone = repos
    (origin / "readme.txt").write_text("2", encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "второй")

    info = update.check_git(clone)
    assert info.available and info.behind == 1
    assert "обновление" in info.message

    applied, _message = update.apply_git(clone)
    assert applied
    assert (clone / "readme.txt").read_text(encoding="utf-8") == "2"
    assert not update.check_git(clone).available


def test_release_check_compares_build_stamp(monkeypatch):
    release = {
        "name": "Последняя сборка", "body": "Коммит: abcdef1234567890",
        "target_commitish": "main", "published_at": "2026-09-09T10:00:00Z",
        "assets": [{"name": "fns-portal.exe", "updated_at": "2026-09-09T10:00:00Z",
                    "browser_download_url": "https://example.invalid/fns-portal.exe"}],
    }
    monkeypatch.setattr(update, "_fetch_json", lambda url: release)

    # тот же коммит — обновления нет
    monkeypatch.setattr(update, "build_info",
                        lambda: {"commit": "abcdef1234567890", "built_at": "2026-09-01T00:00:00Z"})
    assert not update.check_release("owner/repo").available

    # сборка старше релиза — обновление есть
    monkeypatch.setattr(update, "build_info",
                        lambda: {"commit": "0000000000", "built_at": "2026-09-08T00:00:00Z"})
    info = update.check_release("owner/repo")
    assert info.available and info.download_url.endswith("fns-portal.exe")

    # сборка свежее релиза — обновления нет
    monkeypatch.setattr(update, "build_info",
                        lambda: {"commit": "0000000000", "built_at": "2026-09-10T00:00:00Z"})
    assert not update.check_release("owner/repo").available


def test_release_check_survives_no_network(monkeypatch):
    monkeypatch.setattr(update, "_fetch_json", lambda url: None)
    info = update.check_release("owner/repo")
    assert not info.available and "недоступен" in info.message


def test_apply_release_refuses_when_not_frozen(monkeypatch):
    monkeypatch.setattr(update.sys, "frozen", False, raising=False)
    applied, message = update.apply_release("https://example.invalid/x.exe")
    assert not applied and "собранной" in message


def test_release_check_uses_rolling_prerelease_tag(monkeypatch):
    """Скользящая сборка помечена предрелизом, и /releases/latest её не отдаёт."""
    asked: list[str] = []
    tagged = {
        "name": "Последняя сборка", "body": "Коммит: 1111111111111111",
        "published_at": "2026-09-09T06:28:42Z",
        "assets": [{"name": "fns-portal.exe", "updated_at": "2026-09-09T06:28:45Z",
                    "browser_download_url": "https://example.invalid/fns-portal.exe"}],
    }

    def fake_fetch(url: str):
        asked.append(url)
        return tagged if url.endswith("/releases/tags/latest") else None

    monkeypatch.setattr(update, "_fetch_json", fake_fetch)
    monkeypatch.setattr(update, "build_info",
                        lambda: {"commit": "2222222222", "built_at": "2026-09-08T00:00:00Z"})
    info = update.check_release("owner/repo")
    assert asked[0].endswith("/releases/tags/latest")
    assert info.available
