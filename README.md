# Plan Paywall

A Windows desktop app for sending engineering plans to clients behind a pay wall.

The client receives a watermarked preview and your Zelle and/or Venmo QR codes. The final PDF stays on your computer until you confirm payment, then it is emailed automatically.

## How it works

1. **Send:** choose the plan PDF and, optionally, your invoice PDF (the price is read from it), then enter the client's email.
2. **Client pays:** by scanning the Zelle or Venmo QR code in the email.
3. **Mark as paid:** once the payment arrives, click *Mark as paid*. The final PDF is sent.

Every transaction is logged. The log can be searched, exported to CSV, or cleared.

## Install

**[Download PlanSender.zip](https://github.com/brandonkarimi/plan-paywall/releases/latest/download/PlanSender.zip)** (Windows 10/11), unzip the whole folder, and run `PlanSender.exe`.

On first launch, a short setup screen asks for:
- your Zelle and/or Venmo QR code (a screenshot works) or account details
- the email account to send from (for Gmail, use an [app password](https://myaccount.google.com/apppasswords))

Optionally, add your logo in Settings to use it as the preview watermark. A white background is removed automatically.

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
