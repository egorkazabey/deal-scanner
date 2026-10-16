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

from scanner.config import Settings

log = logging.getLogger(__name__)

_ready: bool = False


class BrowserSetupError(Exception):
    """Chromium can't be started here; the message says why (Russian, for the owner)."""


def use_browsers_dir(settings: Settings) -> None:
    """Point Playwright at the persistent download dir, unless the environment already has a browser."""
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(settings.fb_browsers_dir))


async def _probe(settings: Settings) -> str | None:
    """None if Chromium starts, else the error text."""
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, executable_path=settings.fb_browser_path)
            await browser.close()
        return None
    except Exception as exc:  # noqa: BLE001 - any launch error is a setup problem
        return str(exc)


async def _run(*args: str, timeout: float = 900) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "playwright", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=os.environ.copy(),
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
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
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
        log.warning("Chromium missing, downloading into %s", os.environ["PLAYWRIGHT_BROWSERS_PATH"])
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
