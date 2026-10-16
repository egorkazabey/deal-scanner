"""Chromium setup for FB on a bare host image (scanner.browser_setup), with the steps faked."""

import asyncio

import pytest

from scanner import browser_setup as bs
from scanner.config import Settings


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(bs, "_ready", False)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "pw"))


def _fake(monkeypatch, probes, runs=(), apt=False):
    probes, runs, calls = list(probes), list(runs), []

    async def probe(settings):
        return probes.pop(0)

    async def run(*args, timeout=900):
        calls.append(args)
        return runs.pop(0)

    async def apt_chromium(settings):
        calls.append(("apt",))
        if apt:
            settings.fb_browser_path = "/usr/bin/chromium"
        return apt

    monkeypatch.setattr(bs, "_probe", probe)
    monkeypatch.setattr(bs, "_run", run)
    monkeypatch.setattr(bs, "_apt_chromium", apt_chromium)
    return calls


def test_ready_browser_does_nothing(monkeypatch):
    calls = _fake(monkeypatch, [None])
    assert asyncio.run(bs.ensure_chromium(Settings(_env_file=None))) == [] and calls == []
    assert asyncio.run(bs.ensure_chromium(Settings(_env_file=None))) == []  # cached: no second probe


def test_downloads_then_installs_libraries(monkeypatch):
    calls = _fake(monkeypatch, ["Executable doesn't exist at /x", "error while loading shared libraries: libnss3", None],
                  runs=[(0, "ok"), (0, "ok")])
    monkeypatch.setattr(bs.os, "geteuid", lambda: 0)
    done = asyncio.run(bs.ensure_chromium(Settings(_env_file=None)))
    assert done == ["Chromium скачан", "установлены системные библиотеки Chromium"]
    assert calls == [("install", "chromium"), ("install-deps", "chromium")]


def test_download_blocked_falls_back_to_apt(monkeypatch):
    s = Settings(_env_file=None)
    calls = _fake(monkeypatch, ["Executable doesn't exist", None], runs=[(1, "download failure")], apt=True)
    assert asyncio.run(bs.ensure_chromium(s)) == ["Chromium поставлен из apt (скачать сборку Playwright не удалось)"]
    assert calls == [("install", "chromium"), ("apt",)] and s.fb_browser_path == "/usr/bin/chromium"


def test_no_way_to_get_chromium_is_reported(monkeypatch):
    _fake(monkeypatch, ["Executable doesn't exist"], runs=[(1, "download failure")], apt=False)
    with pytest.raises(bs.BrowserSetupError, match="не удалось скачать Chromium"):
        asyncio.run(bs.ensure_chromium(Settings(_env_file=None)))


def test_missing_libraries_without_root(monkeypatch):
    _fake(monkeypatch, ["Host system is missing dependencies to run browsers"])
    monkeypatch.setattr(bs.os, "geteuid", lambda: 1000)
    with pytest.raises(bs.BrowserSetupError, match="root"):
        asyncio.run(bs.ensure_chromium(Settings(_env_file=None)))


def test_snap_stub_is_not_a_browser(tmp_path):
    stub = tmp_path / "chromium-browser"
    stub.write_text("#!/bin/sh\necho 'snap install chromium'\n")
    real = tmp_path / "chromium"
    real.write_bytes(b"\x7fELF" + b"\0" * 100)
    assert not bs._real_binary(str(stub)) and bs._real_binary(str(real)) and not bs._real_binary(str(tmp_path / "x"))


def test_existing_default_browser_is_used(monkeypatch, tmp_path):
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH")
    cache = tmp_path / "cache"
    monkeypatch.setattr(bs, "default_browsers_dir", lambda: cache)
    settings = Settings(_env_file=None, fb_browsers_dir=tmp_path / "data" / "ms-playwright")
    bs.use_browsers_dir(settings)
    assert bs.os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(tmp_path / "data" / "ms-playwright")  # none yet
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH")
    (cache / "chromium-1248").mkdir(parents=True)
    bs.use_browsers_dir(settings)
    assert "PLAYWRIGHT_BROWSERS_PATH" not in bs.os.environ  # `playwright install` already put one there
