from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    BASE_DIR = Path(getattr(sys, "executable")).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

APP_DIR_NAME = "AmazonPokemonSniper"
LEGACY_USER_DATA_DIR = BASE_DIR / "chrome-profile"
PORT = 9226


def _app_data_root() -> Path:
    if sys.platform == "darwin":
        return Path("~/Library/Application Support").expanduser() / APP_DIR_NAME

    if sys.platform.startswith("win"):
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / APP_DIR_NAME
        return BASE_DIR / APP_DIR_NAME

    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home).expanduser() / APP_DIR_NAME
    return Path("~/.local/share").expanduser() / APP_DIR_NAME


def _configured_user_data_dir() -> Path:
    override = os.environ.get("AMAZON_SNIPER_CHROME_PROFILE_DIR")
    if override:
        return Path(override).expanduser()
    return _app_data_root() / "chrome-profile"


USER_DATA_DIR = _configured_user_data_dir()


def ensure_user_data_dir() -> Path:
    target_dir = USER_DATA_DIR.expanduser()
    legacy_dir = LEGACY_USER_DATA_DIR.expanduser()

    if target_dir.exists():
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    if target_dir.resolve() != legacy_dir.resolve() and legacy_dir.exists():
        try:
            shutil.move(str(legacy_dir), str(target_dir))
            print(f"[PROFILE] Moved Chrome profile to {target_dir}")
            return target_dir
        except Exception as exc:
            print(
                f"[WARNING] Failed to move legacy Chrome profile to {target_dir}: {exc}. "
                "Continuing with the legacy profile path for this run."
            )
            legacy_dir.mkdir(parents=True, exist_ok=True)
            return legacy_dir

    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir


def _candidate_paths() -> list[str]:
    if sys.platform.startswith("win"):
        return [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]

    if sys.platform == "darwin":
        return [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            str(
                Path("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome").expanduser()
            ),
        ]

    return []


def find_chrome() -> str | None:
    for candidate in _candidate_paths():
        if os.path.exists(candidate):
            return candidate

    for binary in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(binary)
        if found:
            return found

    return None


def open_chrome_with_profile(url: str) -> None:
    chrome_path = find_chrome()
    if not chrome_path:
        print("[ERROR] Could not find Google Chrome.")
        return

    profile_dir = ensure_user_data_dir()
    chrome_args = [
        f"--remote-debugging-port={PORT}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ]

    if sys.platform == "darwin":
        cmd = [
            "open",
            "-na",
            "Google Chrome",
            "--args",
            *chrome_args,
        ]
    else:
        cmd = [
            chrome_path,
            *chrome_args,
        ]

    try:
        subprocess.Popen(cmd)
    except Exception as exc:  # pragma: no cover - OS launch path
        print(f"[ERROR] Failed to launch Chrome: {exc}")


if __name__ == "__main__":
    open_chrome_with_profile(sys.argv[1] if len(sys.argv) > 1 else "https://www.amazon.com")
