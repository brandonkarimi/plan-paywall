# Plan Delivery: pay-to-unlock engineering plans

Clients review a **watermarked, image-only preview**. The **final PDF never leaves the server until Stripe confirms payment**. Delivery then happens instantly and automatically.

## How it works
1. **The engineer** logs in at `/admin/`, creates a Project (client, price, scope, optional signed agreement), and uploads the final PDF. They can also upload an unsealed PDF to build the preview from.
2. **Saving** analyzes the PDF: page count, sheet index from the bookmarks, and SHA-256 fingerprint. They check the sheet list, then run the **"Send to client"** action.
3. **Sending** generates the preview, **locks** the revision (it can never be changed or deleted), and emails the client a private portal link.
4. **The client** sees the firm's PE license (with a state-board verify link), the deliverables manifest and fingerprint, the scope, and the preview, then pays through Stripe Checkout (card or ACH).
5. **A Stripe webhook** with a verified signature marks the project paid. The app checks the file is retrievable, emails the client the download, and alerts the engineer. ACH unlocks only once the funds clear.
6. **The delivery guarantee:** `check_deliveries` runs every 15 minutes, retries any paid project that couldn't be delivered, and **auto-refunds** it after `DELIVERY_GUARANTEE_HOURS`.
7. **New revisions** after sending become Rev 2, Rev 3, and so on: new fingerprint, client notified. Paid clients get them immediately, and older revisions stay downloadable.

Also handled: refunds issued from the Stripe dashboard re-lock the files, an underpayment never unlocks, duplicate webhooks are processed once, and every download is logged with IP and time. There's an admin action for payments made outside Stripe (check or cash).

## Local setup (Windows)
```powershell
py -3.11 -m venv .venv
.venv\Scripts\pip install -r requirements.txt -r requirements-dev.txt
copy .env.example .env          # then set SECRET_KEY and the Stripe test keys
.venv\Scripts\python manage.py migrate
.venv\Scripts\python manage.py createsuperuser
.venv\Scripts\python manage.py runserver
```
Open http://localhost:8000/admin/ and fill in **Firm profile** first. In DEBUG, emails print to the console.

**Stripe test mode:** the Stripe CLI is in `tools\stripe\` (or install it from [stripe.com/docs/stripe-cli](https://stripe.com/docs/stripe-cli)). In a second terminal:
```powershell
$key = (Select-String '^STRIPE_SECRET_KEY=' .env).Line.Split('=',2)[1]
tools\stripe\stripe.exe listen --api-key $key --events checkout.session.completed,checkout.session.async_payment_succeeded,checkout.session.async_payment_failed,charge.refunded --forward-to localhost:8000/stripe/webhook/
```
Put the `whsec_...` it prints into `STRIPE_WEBHOOK_SECRET` (once; it stays the same). Pay with card `4242 4242 4242 4242` (any future date and CVC).

**Tune the preview on real sheets:**
```
.venv\Scripts\python manage.py make_preview "C:\path\plans.pdf" --dpi 100 --seal-box 0.80,0.70,1.0,1.0
```
Once you've found a seal-cover box that matches the firm's title block, set it as `PREVIEW_SEAL_BOX`.

**Tests:** `.venv\Scripts\python manage.py test plans`

## Production (Render + Cloudflare R2 + Resend + your existing domain)
1. **R2:** create a private bucket and an API token, then set `USE_S3=true` and the `S3_*` vars. Downloads become 5-minute presigned URLs.
2. **Render:** create a new Blueprint from this repo (`render.yaml`). That gives you the web service, the 15-minute cron job, and Postgres. Fill in the `sync: false` env vars, then run `python manage.py createsuperuser` in the Render shell.
3. **Domain** (keep your registrar, add records only):
   - `CNAME plans → <service>.onrender.com`, then add `plans.yourfirm.com` as a custom domain in Render (HTTPS is automatic).
   - Add the SPF/DKIM/DMARC records that Resend gives you, so mail from `plans@yourfirm.com` lands in the inbox. **Merge** into an existing SPF record rather than adding a second one. Don't touch MX, nameservers, or the root `A` record.
   - Set `SITE_URL=https://plans.yourfirm.com` and `ALLOWED_HOSTS=plans.yourfirm.com`.
4. **Stripe (live):** in the engineer's account, add a webhook endpoint `https://plans.yourfirm.com/stripe/webhook/` with the events `checkout.session.completed`, `checkout.session.async_payment_succeeded`, `checkout.session.async_payment_failed` and `charge.refunded`. Then set the live `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET`. Set the business name, statement descriptor, and branding in Stripe settings.

## Layout
```
config/                    settings (all env-driven), urls
plans/models.py            FirmProfile, Project, PlanRevision (immutable once locked), DownloadEvent, ProcessedStripeEvent
plans/admin.py             engineer dashboard, actions, staff-only file viewer
plans/views.py             client portal, preview, checkout, gated download, resend, webhook
plans/services/preview.py  rasterize + watermark → image-only PDF
plans/services/pdfinfo.py  SHA-256, page count, sheet index
plans/services/delivery.py publish/lock, send, mark paid, deliver, guarantee check
plans/services/payments.py Stripe Checkout, webhook processing, refunds
plans/templates/plans/     portal page + email templates
```

---

# Plan Sender desktop app (no server)
A Windows app for one engineer: pick a PDF, enter the client's email and price, and click **Send with pay wall**. The client gets a watermarked preview and a Stripe pay link. The app watches Stripe and **emails the final PDF automatically once they pay**. The final file never leaves this PC before payment. A **Transactions** tab keeps a saved log of every send and update, with CSV export.

- **Run from source:** `.venv\Scripts\pythonw run_desktop.py` (add `--tray` to start hidden).
- **Build the .exe:** `powershell -ExecutionPolicy Bypass -File desktop\build.ps1`. This produces `dist\PlanSender\PlanSender.exe` and `dist\PlanSender.zip`, which you give to the engineer to unzip and double-click.
- **First run:** use Settings to add (1) the Stripe secret key, (2) an email account (for Gmail, an app password from myaccount.google.com/apppasswords), and (3) the signature details. Turn on "Start with Windows" so payments are processed whenever the PC is on.
- **Payment methods** (Settings → How clients can pay): any mix of
  - **Stripe** (card or bank): detected automatically, and the plans go out within minutes.
  - **Zelle** (no fees) and **Venmo**: the email gives the client exact instructions and a reference code (`PLN-<id>`), plus a pre-filled Venmo link. These apps have no API, so when the money arrives you click **Transactions → Mark as paid** and the final PDF is emailed immediately. Only mark a payment paid once you see it in your bank or Venmo app. Venmo requires a business profile for payments for work.
- **Data:** stored in `%APPDATA%\PlanSender` (database, copies of sent PDFs, log). Secrets are in Windows Credential Manager.
- **Delivery timing:** the app checks Stripe every 2 minutes while it's running (it sits in the tray when the window is closed). If the PC is off when a client pays, the plans go out when it next starts. Clients are told "usually within minutes, at most 1 business day".
- **Tests:** `.venv\Scripts\python -m pytest desktop\tests`
