# Plan Paywall

A Windows desktop app for sending engineering plans to clients behind a pay wall.

The client receives a watermarked preview and payment instructions (Zelle or Venmo). The final PDF stays on your computer until you confirm payment, then it is emailed automatically.

## How it works

1. **Send:** choose the plan PDF and, optionally, your invoice PDF (the price is read from it), then enter the client's email.
2. **Client pays:** by Zelle or Venmo, using the reference code in the email.
3. **Mark as paid:** once the payment arrives, click *Mark as paid*. The final PDF is sent.

Every transaction is logged. The log can be searched, exported to CSV, or cleared.

## Install

Download `PlanSender.zip`, unzip it, and run `PlanSender.exe`.

On first launch, a short setup screen asks for:
- your Zelle and/or Venmo details
- the email account to send from (for Gmail, use an [app password](https://myaccount.google.com/apppasswords))

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
