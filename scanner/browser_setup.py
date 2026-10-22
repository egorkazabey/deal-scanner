"""Chromium for the Facebook source on a host whose image has none.

The host builds its own image (python:3.12-slim) and only installs the Python dependencies, so the
browser is set up at runtime, once: `playwright install chromium` downloads it into
`data/ms-playwright` (the persistent volume, so a redeploy does not download it again), and when the
system libraries are missing, `playwright install-deps chromium` adds them with apt (root only; after
a redeploy this step runs again, the image is fresh).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from scanner.config import Settings

log = logging.getLogger(__name__)

_ready: bool = False


class BrowserSetupError(Exception):
    """Chromium can't be started here; the message says why (Russian, for the owner)."""


def default_browsers_dir() -> Path:
    """Where `playwright install` puts browsers when PLAYWRIGHT_BROWSERS_PATH is not set."""
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Caches" / "ms-playwright"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local")) / "ms-playwright"
    return Path(os.environ.get("XDG_CACHE_HOME", home / ".cache")) / "ms-playwright"


def use_browsers_dir(settings: Settings) -> None:
    """Use the persistent download dir (data/ms-playwright) unless a browser is already set up.

    On a PC `python -m playwright install chromium` puts Chromium into the default cache: keep using it.
    On the host there is none, so the download goes to the persistent volume.
    """
    if "PLAYWRIGHT_BROWSERS_PATH" in os.environ:
        return
    if any(default_browsers_dir().glob("chromium-*")):
        return
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(settings.fb_browsers_dir)


async def _probe(settings: Settings) -> str | None:
    """None if Chromium starts, else the error text."""
    from playwright.async_api import async_playwright

    async def launch() -> None:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, executable_path=settings.fb_browser_path,
                                               timeout=60_000)
            await browser.close()

    try:
        await asyncio.wait_for(launch(), timeout=120)
        return None
    except asyncio.TimeoutError:
        return "Chromium не ответил за 2 минуты при запуске"
    except Exception as exc:  # noqa: BLE001 - any launch error is a setup problem
        return str(exc)


async def _run(*args: str, timeout: float = 900) -> tuple[int, str]:
    # No terminal on a server: apt must never wait for an answer (debconf prompts, "Do you want to continue?").
    env = os.environ | {"DEBIAN_FRONTEND": "noninteractive"}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "playwright", *args, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, "timeout"
    return proc.returncode or 0, out.decode(errors="replace")[-1500:]


_APT_CHROMIUM = ("/usr/bin/chromium", "/usr/bin/chromium-browser")


def _real_binary(path: str) -> bool:
    """Debian ships a real /usr/bin/chromium; Ubuntu's apt package is only a stub that wants snap."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return False
    return b"snap install" not in head


async def _apt_chromium(settings: Settings) -> bool:
    """Fallback: Debian's chromium package (root only); Playwright then uses it via executable_path."""
    if not (hasattr(os, "geteuid") and os.geteuid() == 0):
        return False
    proc = await asyncio.create_subprocess_shell(
        "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends chromium",
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 900)
    except asyncio.TimeoutError:
        proc.kill()
        return False
    path = next((p for p in _APT_CHROMIUM if _real_binary(p)), None)
    if proc.returncode or path is None:
        log.error("apt chromium failed: %s", out.decode(errors="replace")[-500:])
        return False
    settings.fb_browser_path = path  # this process; FacebookClient reads it from settings
    return True


async def ensure_chromium(settings: Settings) -> list[str]:
    """Make sure Chromium starts; returns what was done (for the log / Telegram)."""
    global _ready
    if _ready:
        return []
    use_browsers_dir(settings)
    done: list[str] = []
    error = await _probe(settings)
    if error and settings.fb_auto_install and "Executable doesn't exist" in error:
        log.warning("Chromium missing, downloading into %s",
                    os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or default_browsers_dir())
        code, out = await _run("install", "chromium")
        if code == 0:
            done.append("Chromium скачан")
            error = await _probe(settings)
        elif await _apt_chromium(settings):
            done.append("Chromium поставлен из apt (скачать сборку Playwright не удалось)")
            error = await _probe(settings)
        else:
            raise BrowserSetupError(f"не удалось скачать Chromium: {out[-300:]}")
    if error and settings.fb_auto_install and ("shared librar" in error or "missing dependencies" in error):
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            raise BrowserSetupError("Chromium не хватает системных библиотек, а прав root нет для их установки")
        log.warning("Chromium system libraries missing, installing with apt")
        code, out = await _run("install-deps", "chromium")
        if code:
            raise BrowserSetupError(f"не удалось установить библиотеки Chromium: {out[-300:]}")
        done.append("установлены системные библиотеки Chromium")
        error = await _probe(settings)
    if error:
        raise BrowserSetupError(f"Chromium не запускается: {error[:300]}")
    _ready = True
    return done
