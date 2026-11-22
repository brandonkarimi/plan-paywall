# Plan Paywall

A Windows desktop app for sending engineering plans to clients behind a pay wall.

The client receives a watermarked preview and your Zelle and/or Venmo QR codes. The final PDF stays on your computer until the payment arrives, then it is emailed automatically.

## How it works

1. **Send:** choose the plan PDF and, optionally, your invoice PDF, enter the client's name, email and the price, and optionally write the subject and message of the preview and final emails (left empty, standard wording is used).
2. **Client pays:** by Zelle or Venmo. No memo or note is needed.
3. **Plans go out:** with automatic payments on, Plan Sender spots the Wells Fargo (Zelle) or Venmo payment email within seconds, even while the app is closed, and emails the final PDF. Payments it isn't sure about (e.g. $10 or more short, or a different payer name) wait for your OK in Transactions. You can also click *Mark as paid* yourself.

Every transaction is logged. The log can be searched, exported to CSV, or cleared.

## Install

**[Download PlanSender.zip](https://github.com/brandonkarimi/plan-paywall/releases/latest/download/PlanSender.zip)** (Windows 10/11), unzip the whole folder, and run `PlanSender.exe`.

On first launch, a short setup screen asks for:
- your Zelle and/or Venmo QR code (a screenshot works) or account details
- the email account to send from (for Gmail, use an [app password](https://myaccount.google.com/apppasswords))

Optionally, add your logo in Settings to use it as the preview watermark. A white background is removed automatically.

### Automatic payments

In Settings → Automatic payments, turn it on and enter the email that gets your Zelle (Wells Fargo) and Venmo payment alerts, with its app password. Make sure Wells Fargo sends Zelle "money received" alerts by email and Venmo emails you for payments received. A small background checker then starts with Windows; it only reads the inbox while a plan is unpaid.

## Development

```powershell
py -3.11 -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python run_desktop.py          # run the app
.venv\Scripts\python -m pytest desktop\tests  # run tests
powershell -ExecutionPolicy Bypass -File desktop\build.ps1   # build the .exe
```

## Notes

- Data is stored locally in `%APPDATA%\PlanSender`. Email passwords are kept in Windows Credential Manager.
- Previews are image-only and watermarked, so they can't be edited or reused as final plans.
- QR codes and the logo are stored only on your computer, never in the app download.
- Only payment emails genuinely sent by Wells Fargo or Venmo are trusted, and plans are never sent for a payment that's $10 or more short.
