"""Paths, settings and secrets.

Settings live in %APPDATA%\\PlanSender\\settings.json; email passwords live in Windows Credential Manager.
"""
import json
import os
from pathlib import Path

import keyring

APP_NAME = "PlanSender"
KEYRING_SERVICE = "PlanSender"
ATTACH_LIMIT_BYTES = 18 * 1024 * 1024  # ~25 MB once email-encoded; larger finals need a share link

PAYMENT_METHODS = {"zelle": "Zelle", "venmo": "Venmo"}
MANUAL_METHODS = ["Zelle", "Venmo", "Check", "Cash", "Other"]

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

# Standard wording of the two emails. The preview one can be replaced per send on the Send page.
# {client}, {project} and {amount} are filled in; blank ones drop out ("Hello {client}," → "Hello,").
EMAIL_DEFAULTS = {
    "preview_subject": "Preview: {project}",
    "preview_body": "Hello {client},\n\nHere is a preview of your plans for {project}. To receive the full PDF, please "
                    "pay using one of the options below. Once payment is received, the full PDF will be emailed to you.",
    "final_subject": "Plans: {project}",
    "final_body": "Hello {client},\n\nThank you for your payment. Here are the full plans for {project}.",
}

DEFAULT_SETTINGS = {
    "accounts": [],          # [{"email", "name", "preset", "host", "port", "security"}]; passwords in keyring
    "default_from": "",
    # Email signature; "name" includes credentials as the engineer wants them shown (e.g. "Pat Lee, PE").
    "signature": {"name": "", "firm": "", "license": "", "phone": "", "email": "", "street": "", "city": ""},
    "zelle": {"enabled": False, "recipient": "", "name": ""},   # email/phone clients send to + name shown in Zelle
    "venmo": {"enabled": False, "username": ""},
    # Automatic payments: the inbox(es) that get the Wells Fargo (Zelle) and Venmo payment emails; an empty Venmo
    # email means the same inbox as Zelle. Passwords live in Windows Credential Manager as "imap:<email>".
    "auto_payments": {"enabled": False, "zelle": {"email": ""}, "venmo": {"email": ""}},
    "preview_dpi": 100,
    "seal_box": "",
    "theme": "light",        # "light" (blue and white) or "dark"
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


def remove_tree(path) -> None:
    """Delete a stored-files folder, including the read-only final PDFs (Windows won't delete those as-is)."""
    import os
    import shutil
    import stat

    def clear_readonly(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if Path(path).exists():
        try:
            shutil.rmtree(path, onerror=clear_readonly)
        except OSError:
            pass


# Pictures the engineer adds in Settings. Kept on this PC only (never bundled into the app or the repo).
IMAGES = {
    "zelle_qr": "Zelle QR code",
    "venmo_qr": "Venmo QR code",
    "watermark": "Watermark logo",
    "signature_logo": "Signature logo",
}


def images_dir() -> Path:
    path = data_dir() / "images"
    path.mkdir(parents=True, exist_ok=True)
    return path


def image_path(slot):
    """The stored PNG for ``slot`` if one has been added, else None."""
    path = images_dir() / f"{slot}.png"
    return path if slot in IMAGES and path.is_file() else None


def log_path() -> Path:
    return data_dir() / "plansender.log"


def load_settings() -> dict:
    path = data_dir() / "settings.json"
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            for k, v in saved.items():
                if k in DEFAULT_SETTINGS:
                    settings[k] = {**DEFAULT_SETTINGS[k], **v} if isinstance(DEFAULT_SETTINGS[k], dict) else v
        except (OSError, ValueError):
            pass
    return settings


def save_settings(settings: dict):
    clean = {k: settings.get(k, v) for k, v in DEFAULT_SETTINGS.items()}
    (data_dir() / "settings.json").write_text(json.dumps(clean, indent=2), encoding="utf-8")


def enabled_methods(settings=None) -> list:
    settings = settings or load_settings()
    return [m for m in PAYMENT_METHODS if settings[m].get("enabled") and method_ready(m, settings)]


def method_ready(method, settings) -> bool:
    """A payment method can be offered once clients have a way to pay: the typed details or a QR code."""
    typed = settings[method].get("recipient" if method == "zelle" else "username")
    return bool(typed or image_path(f"{method}_qr"))


def account_for(email: str, settings=None):
    settings = settings or load_settings()
    for acct in settings["accounts"]:
        if acct["email"].lower() == (email or "").lower():
            return acct
    return None


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


def smtp_password(email: str) -> str:
    return get_secret(f"smtp:{email.lower()}")
