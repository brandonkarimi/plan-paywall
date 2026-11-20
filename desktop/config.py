"""Paths, settings and secrets for the desktop app.

Non-secret settings live in %APPDATA%\\PlanSender\\settings.json.
Secrets (Stripe key, email passwords) live in Windows Credential Manager via `keyring`.
"""
import json
import os
import sys
from pathlib import Path

import keyring

APP_NAME = "PlanSender"
KEYRING_SERVICE = "PlanSender"
ATTACH_LIMIT_BYTES = 18 * 1024 * 1024  # ~25 MB once email-encoded; larger finals need a share link

SMTP_PRESETS = {
    "gmail": {"label": "Gmail / Google Workspace", "host": "smtp.gmail.com", "port": 465, "security": "ssl",
              "help": "Use an app password: myaccount.google.com/apppasswords (needs 2-Step Verification)."},
    "outlook": {"label": "Outlook.com / Hotmail", "host": "smtp-mail.outlook.com", "port": 587, "security": "starttls",
                "help": "Use your password, or an app password if two-step verification is on."},
    "office365": {"label": "Microsoft 365 (work email)", "host": "smtp.office365.com", "port": 587, "security": "starttls",
                  "help": "Your IT admin may need to enable 'Authenticated SMTP' for this mailbox."},
    "godaddy": {"label": "GoDaddy email", "host": "smtpout.secureserver.net", "port": 465, "security": "ssl",
                "help": "Your GoDaddy email address and its password."},
    "custom": {"label": "Other (custom SMTP)", "host": "", "port": 587, "security": "starttls", "help": ""},
}

DEFAULT_SETTINGS = {
    "accounts": [],          # [{"email", "name", "preset", "host", "port", "security"}]; passwords in keyring
    "default_from": "",
    "signature": {"name": "", "firm": "", "license": "", "phone": ""},
    "allow_ach": True,
    "start_with_windows": False,
    "poll_seconds": 120,
    "preview_dpi": 100,
    "seal_box": "",
}


def data_dir() -> Path:
    base = os.environ.get("PLANSENDER_HOME") or Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME
    path = Path(base)
    (path / "files").mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / "plansender.db"


def files_dir(tx_id) -> Path:
    path = data_dir() / "files" / str(tx_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_path() -> Path:
    return data_dir() / "plansender.log"


# ------------------------------------------------------------------ settings
def load_settings() -> dict:
    path = data_dir() / "settings.json"
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            settings.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
        except (OSError, ValueError):
            pass
    return settings


def save_settings(settings: dict):
    clean = {k: settings.get(k, v) for k, v in DEFAULT_SETTINGS.items()}
    (data_dir() / "settings.json").write_text(json.dumps(clean, indent=2), encoding="utf-8")
    set_start_with_windows(bool(clean["start_with_windows"]))


def account_for(email: str, settings=None):
    settings = settings or load_settings()
    for acct in settings["accounts"]:
        if acct["email"].lower() == (email or "").lower():
            return acct
    return None


# ------------------------------------------------------------------ secrets
def get_secret(name: str) -> str:
    try:
        return keyring.get_password(KEYRING_SERVICE, name) or ""
    except Exception:
        return ""


def set_secret(name: str, value: str):
    if value:
        keyring.set_password(KEYRING_SERVICE, name, value)
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, name)
        except Exception:
            pass


def stripe_key() -> str:
    return get_secret("stripe_secret_key")


def smtp_password(email: str) -> str:
    return get_secret(f"smtp:{email.lower()}")


# ------------------------------------------------------------------ start with Windows
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command(tray=True) -> str:
    if getattr(sys, "frozen", False):
        cmd = f'"{sys.executable}"'
    else:
        pyw = Path(sys.executable).with_name("pythonw.exe")
        script = Path(__file__).resolve().parent.parent / "run_desktop.py"
        cmd = f'"{pyw if pyw.exists() else sys.executable}" "{script}"'
    return cmd + (" --tray" if tray else "")


def set_start_with_windows(enabled: bool):
    if sys.platform != "win32" or os.environ.get("PLANSENDER_HOME"):  # never touch the registry from tests
        return
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, launch_command(tray=True))
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
