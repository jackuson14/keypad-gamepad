"""updater.py - check GitHub Releases for a newer build and download it.

Pure logic with NO Tkinter, so it's unit-testable and safe to call from a worker
thread; the GUI (analog_gui.py) marshals results back to the Tk thread itself.
Stdlib only, plus `certifi` for TLS: a frozen PyInstaller .exe on a clean Windows
machine does NOT use the Windows cert store and otherwise fails HTTPS with
CERTIFICATE_VERIFY_FAILED. `check_for_update` never raises - failures are returned
in `UpdateInfo.error`.
"""

from __future__ import annotations

import json
import os
import re
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from version import __version__

GITHUB_OWNER = "jackuson14"
GITHUB_REPO = "keypad-gamepad"
ASSET_NAME = "keypad-gamepad-analog.exe"

API_LATEST_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
USER_AGENT = f"keypad-gamepad/{__version__} (+https://github.com/{GITHUB_OWNER}/{GITHUB_REPO})"


# --------------------------------------------------------------------------- TLS

def _ssl_context() -> ssl.SSLContext:
    """A verifying TLS context that works both from source and frozen.

    Prefers certifi's CA bundle (bundled into the .exe via analog_gui.spec); falls
    back to the platform default when certifi isn't installed (dev environments)."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


# ----------------------------------------------------------------- version compare

_VER_RE = re.compile(r"^[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+](.+))?$")


def _parse(tag: str) -> tuple[tuple[int, int, int], str | None] | None:
    """'v1.2.3-rc1' -> ((1,2,3), 'rc1'); '1.2' -> ((1,2,0), None); junk -> None."""
    if not tag:
        return None
    m = _VER_RE.match(tag.strip())
    if not m:
        return None
    nums = tuple(int(x) if x else 0 for x in m.groups()[:3])
    return nums, m.group(4)  # type: ignore[return-value]


def parse_version(tag: str) -> tuple[int, int, int] | None:
    """The numeric release triple of a tag/version string, or None if unparseable."""
    p = _parse(tag)
    return p[0] if p else None


def is_newer(current: str, candidate: str) -> bool:
    """True iff `candidate` is a strictly newer *release* than `current`.

    A pre-release (e.g. 0.2.2-rc1) ranks below the equal-numbered final (0.2.2)."""
    c = _parse(current)
    n = _parse(candidate)
    if not n:
        return False
    if not c:
        return True  # can't read our own version; treat any parseable release as new
    (cn, cpre), (nn, npre) = c, n
    if nn != cn:
        return nn > cn
    if cpre is None and npre is not None:
        return False  # candidate is a pre-release of the same number we already ship
    if cpre is not None and npre is None:
        return True   # we're on a pre-release; the final of that number is newer
    if cpre is not None and npre is not None:
        return npre > cpre  # rough lexical ordering between two pre-releases
    return False  # identical final releases


# ------------------------------------------------------------------- check / fetch

@dataclass(frozen=True)
class UpdateInfo:
    current_version: str
    latest_version: str | None       # tag with the leading "v" stripped, e.g. "0.2.2"
    is_update_available: bool
    asset_url: str | None            # browser_download_url for ASSET_NAME
    asset_name: str | None
    release_url: str                 # release html_url, or the releases page
    notes: str | None                # release body (markdown); truncate for display
    error: str | None = None         # human-readable failure reason; None on success


class _NotFound(Exception):
    """The repo has no published full release yet (HTTP 404 from /releases/latest)."""


def _get_json(url: str, *, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise _NotFound() from e
        if e.code == 403 and e.headers.get("X-RateLimit-Remaining") == "0":
            raise RuntimeError("GitHub rate limit reached - try again later.") from e
        raise


def _pick_asset(assets: list[dict]) -> tuple[str | None, str | None]:
    """Find the .exe asset: exact name, then case-insensitive, then any .exe."""
    def name(a: dict) -> str:
        return a.get("name") or ""
    for group in (
        [a for a in assets if name(a) == ASSET_NAME],
        [a for a in assets if name(a).lower() == ASSET_NAME.lower()],
        [a for a in assets if name(a).lower().endswith(".exe")],
    ):
        if group:
            return group[0].get("browser_download_url"), group[0].get("name")
    return None, None


def _friendly_error(e: Exception) -> str:
    if isinstance(e, RuntimeError):
        return str(e)
    if isinstance(e, ssl.SSLError):
        return f"Secure connection failed: {e}"
    if isinstance(e, (urllib.error.URLError, socket.timeout, TimeoutError)):
        return "Could not reach GitHub. Check your internet connection."
    if isinstance(e, json.JSONDecodeError):
        return "GitHub returned an unexpected response."
    return f"Update check failed: {e}"


def check_for_update(current_version: str = __version__, *, timeout: float = 8.0) -> UpdateInfo:
    """Query GitHub for the latest release. Never raises."""
    base = dict(current_version=current_version, latest_version=None,
                is_update_available=False, asset_url=None, asset_name=None,
                release_url=RELEASES_PAGE_URL, notes=None, error=None)
    try:
        data = _get_json(API_LATEST_URL, timeout=timeout)
    except _NotFound:
        return UpdateInfo(**base)  # no full release published yet -> nothing to offer
    except Exception as e:  # noqa: BLE001 - funnel every failure into .error
        return UpdateInfo(**{**base, "error": _friendly_error(e)})

    tag = (data.get("tag_name") or "").strip()
    parsed = parse_version(tag)
    latest = tag[1:] if (parsed and tag[:1] in "vV") else (tag or None)
    asset_url, asset_name = _pick_asset(data.get("assets") or [])
    return UpdateInfo(
        current_version=current_version,
        latest_version=latest,
        is_update_available=bool(parsed) and is_newer(current_version, tag),
        asset_url=asset_url,
        asset_name=asset_name,
        release_url=data.get("html_url") or RELEASES_PAGE_URL,
        notes=(data.get("body") or None),
        error=None,
    )


def download_asset(url: str, dest_dir: Path | str, *, filename: str | None = None,
                   progress_cb: Callable[[int, int | None], None] | None = None,
                   timeout: float = 30.0, chunk: int = 64 * 1024) -> Path:
    """Stream `url` to dest_dir/filename, reporting progress. Atomic via a .part file.

    progress_cb(bytes_done, total_or_None) is called on this (caller's) thread; the
    GUI marshals it onto the Tk thread. `total` is None when Content-Length is absent.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    if filename is None:
        filename = url.rsplit("/", 1)[-1] or ASSET_NAME
    final = dest_dir / filename
    part = dest_dir / (filename + ".part")

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
        cl = resp.headers.get("Content-Length")
        total = int(cl) if cl and cl.isdigit() else None
        done = 0
        with open(part, "wb") as f:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                f.write(buf)
                done += len(buf)
                if progress_cb:
                    progress_cb(done, total)
    part.replace(final)  # only surfaces a complete file under the real name
    return final


# ------------------------------------------------------------------------- helpers

def default_download_dir() -> Path:
    d = Path.home() / "Downloads"
    return d if d.exists() else Path.home()


def running_exe_path() -> Path | None:
    """Path of the running .exe when frozen (so callers never overwrite it), else None."""
    return Path(sys.executable) if getattr(sys, "frozen", False) else None


def reveal_in_explorer(path: Path | str) -> None:
    """Open Explorer with `path` selected; fall back to opening its folder."""
    path = Path(path)
    try:
        # explorer returns exit code 1 even on success, so fire-and-forget.
        subprocess.Popen(["explorer", f"/select,{path}"])
    except Exception:
        try:
            os.startfile(str(path.parent))  # type: ignore[attr-defined]
        except Exception:
            webbrowser.open(path.parent.as_uri())
