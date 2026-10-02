# SwyfTech Client Portal

A self-hosted client portal for SwyfTech LLC. Clients sign in with a password plus an authenticator app and can:

- **Ask for help** and follow their requests, with replies from SwyfTech by email and in the portal, and attach screenshots, photos and files
- **View and pay invoices** online through Stripe (card or bank account), or see them as paid when you record a check
- **Download documents** like the MSA, proposals and policies
- **Read how-to guides** you write in Markdown
- **Book on-site visits** from your open times, and add them to their calendar
- **Rate a request** with one click when it's resolved
- **Read a monthly report** of what SwyfTech did for them
- **See their equipment** from Snipe-IT, with warranty dates, and pick a device when asking for help (optional)

You run it from the SwyfTech staff side: add clients, invite their people, answer requests (with internal notes), confirm and schedule visits, build and send invoices, upload documents and publish guides.

It's one small FastAPI app with a SQLite database. No outside services are required except Stripe for online payments and an SMTP server for email, and both are optional.

## Who sees what

Each client business can have several people. The **Owner** decides what everyone else can see from the **Your team** page.

| Access | Requests | Invoices and payments | On-site visits | Documents | Manage team |
|---|---|---|---|---|---|
| Owner | Everyone's at their business | Yes | Book and cancel | All | Yes |
| Billing | Their own | Yes | Book and cancel | All | No |
| Team member | Their own | No | No | Ones marked "Everyone" | No |

Monthly reports are emailed to Owners and can be read in the portal by Owners and Billing people.

With Snipe-IT connected, Owners and Billing see every device at their business. Team members see devices checked out to them plus shared ones checked out to a location, like the office printer.

Documents marked **Owner and Billing only** (good for contracts with pricing) are hidden from team members.

## Install on Proxmox

1. Push this folder to GitHub as `wolfej4/swyftech-portal`. The installer downloads from there, so the repo needs to be **public**. If you'd rather keep it private, see "Private repo" below.
2. In the Proxmox host shell, run:

   ```bash
   bash -c "$(curl -fsSL https://raw.githubusercontent.com/wolfej4/swyftech-portal/main/ct/swyftech-portal.sh)"
   ```

3. Choose **Default** (1 core, 1 GB RAM, 4 GB disk, DHCP, Debian 12, unprivileged) or **Advanced**, then enter your staff email.

The script creates the container, installs the portal as a systemd service on port 8000, and prints your first staff password. It's also saved inside the container at `/root/swyftech-portal.creds`.

Like the Community Scripts containers, the container has no root password by default, and its **Console** tab in Proxmox opens already logged in as root. That's only reachable by someone already signed in to Proxmox, who could get the same root shell with `pct enter <id>`. To require a password instead, choose **Advanced** and set one. The console then shows the portal's address and the useful commands each time it opens.

To use a fork or a different branch: `REPO=you/your-fork BRANCH=dev bash -c "$(curl ...)"`.

Why not the official Community Scripts? Their shared `build.func` is built to fetch install scripts from their own repository, so a custom app needs its own launcher. This one follows the same flow and adds an `update` command inside the container, just like theirs.

### Private repo

Either clone it into the container yourself (`git clone https://<token>@github.com/wolfej4/swyftech-portal.git /opt/swyftech-portal`) and then run `install/swyftech-portal-install.sh`, or keep only the two scripts public.

## Put it behind HTTPS (required before clients use it)

Stripe needs a public HTTPS address to send payment confirmations, and sign-in cookies are locked to HTTPS once `BASE_URL` starts with `https://`.

1. Point a hostname like `portal.swyftech.net` at the container's port 8000 with Pangolin, Nginx Proxy Manager, Caddy, or a Cloudflare Tunnel. Don't put a sign-in wall (SSO) in front of it: clients sign in to the portal itself, and Stripe, rating links and calendar subscriptions need to reach it directly.
2. In the container, edit `/opt/swyftech-portal/.env` and set `BASE_URL=https://portal.swyftech.net`.
3. Run `systemctl restart swyftech-portal`.

After that, always sign in through the HTTPS address. Signing in through `http://<ip>:8000` won't work because the browser won't send a secure-only cookie over plain HTTP.

If you put Cloudflare Access in front of the portal, add a bypass policy for `/stripe/webhook` so Stripe can reach it.

## Admin

Admins see an **Admin** link in the sidebar with four sections:

- **Business details:** business name, support email and phone, sign-in tagline, invoice footer and due days.
- **Email:** SMTP server, port, security, login and sender, plus where SwyfTech alerts go. **Send test email** sends right away and shows the server's reason if it fails. The SMTP password is encrypted with a key derived from `SECRET_KEY`, so the database or its backups alone don't reveal it. If you ever change `SECRET_KEY`, re-enter the password.
- **SwyfTech staff:** invite staff, choose Admin or Technician, send a password reset link, reset someone's authenticator, unlock them or turn them off. You can't change your own access, and there's always at least one active Admin.
- **Sign-in and security:** shortest password, wrong tries before a pause, pause length, and how long people stay signed in (applies right away). **Sign everyone out** ends every session but yours. Authenticator apps are always required.

Anything saved here overrides `.env` without a restart. Fields you've changed show a **Reset to default** link that goes back to the `.env` value. Every change is recorded in the audit log.

| Staff role | Requests, clients, visits, documents, guides | Invoices, reports, visit hours | Admin |
|---|---|---|---|
| Admin | Yes | Yes | Yes |
| Technician | Yes | No | No |

The first staff account (from the installer or `portal-cli create-staff`) is an Admin. Add `--technician` to create a Technician from the command line. Client people's pages also have a **Password reset link** button, which is handy when email isn't set up: the link is shown on screen to copy.

## Settings

Everything lives in `/opt/swyftech-portal/.env`. Settings changed under Admin take priority over it. `example.env` in the repo lists every option with notes. Restart the service after changes.

| Setting | What it does |
|---|---|
| `BASE_URL` | The address clients use. Links in emails are built from it. |
| `BUSINESS_NAME`, `SUPPORT_EMAIL`, `SUPPORT_PHONE` | Shown on invoices, emails and the "Something urgent?" box |
| `STAFF_NOTIFY_EMAIL` | Where new-request, reply and payment alerts go |
| `TAGLINE` | The line under the logo on the sign-in page |
| `INVOICE_PREFIX`, `DEFAULT_NET_DAYS`, `PAYMENT_TERMS_NOTE` | Invoice numbering (SWY-0001), default due date, footer text |
| `TIMEZONE` | Defaults to `America/Chicago` (Central, for Okaloosa County) |
| `SMTP_*` | Outgoing email. Leave blank to skip email. |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | Online payments. Leave blank to hide the Pay button. |

## Email

Without email, the portal still works: invite links appear on screen with a Copy button so you can text or email them yourself, and password resets fall back to "contact SwyfTech".

With email, the portal sends invites, password resets, new-invoice notices (to the client's Owner and Billing people), replies on requests (to the person who opened it), and alerts to you.

For Microsoft 365: `SMTP_HOST=smtp.office365.com`, `SMTP_PORT=587`, `SMTP_TLS=starttls`, and a mailbox with SMTP AUTH enabled. Microsoft is retiring basic-auth SMTP for Exchange Online, so if your tenant blocks it, use a sending service like SMTP2GO, Postmark or Amazon SES instead. They all work with the same settings.

## Stripe

1. In the Stripe Dashboard, copy your **Secret key** into `STRIPE_SECRET_KEY`. Start with the test key (`sk_test_...`).
2. Under **Developers > Webhooks**, add an endpoint at `https://portal.swyftech.net/stripe/webhook` with these events:
   - `checkout.session.completed`
   - `checkout.session.async_payment_succeeded`
3. Copy that endpoint's **Signing secret** (`whsec_...`) into `STRIPE_WEBHOOK_SECRET`.
4. Under **Settings > Payment methods**, turn on **ACH Direct Debit** if you want clients to pay by bank account. For larger monthly invoices this usually costs you less than card fees.
5. Restart the service and pay a test invoice with card `4242 4242 4242 4242`.

How it works: the Pay button sends the client to a Stripe-hosted checkout page for the exact invoice total, so card and bank details never touch your server. When Stripe confirms payment, the webhook checks the signature and the amount, then marks the invoice paid. Bank payments take a few days to clear and are marked paid when they do.

## Attachments and file storage

Clients can attach up to 5 files (25 MB each) when they open a request or reply: screenshots, phone photos, PDFs, Office files, text and log files, zips, and saved emails. Photos show as thumbnails in the thread. You can attach files too, including on internal notes, which clients never see. Programs and scripts are refused, and a rejected file never wipes what the person typed. Change the limits with `MAX_UPLOAD_MB` and `MAX_FILES_PER_MESSAGE`, and allow the same size in your reverse proxy.

Request attachments and Documents share one storage setting. By default, files live on the container's disk in `data/uploads`. To use S3-compatible storage instead:

1. Create a bucket and an access key that can read, write and delete in it.
2. Add to `.env`:

   ```
   S3_ENDPOINT_URL=http://10.0.10.30:3900
   S3_BUCKET=swyftech-portal
   S3_ACCESS_KEY_ID=...
   S3_SECRET_ACCESS_KEY=...
   S3_REGION=garage
   ```

   For Garage, the region must match `s3_region` in its config (usually `garage`). SeaweedFS accepts any region. For Backblaze B2, Cloudflare R2 or AWS, use the endpoint and region they give you (R2 uses `auto`).
3. Restart the portal, then run `portal-cli check-storage`. It writes, reads and deletes a test file and tells you where files are going.
4. Run `portal-cli move-files-to-s3` to move files that were saved before you switched. Each file is checked in S3 before its local copy is removed (`--keep-local` leaves the local copies).

Which store? MinIO's community edition is no longer maintained, so for self-hosting look at Garage (lightweight, made for small setups) or SeaweedFS (has an admin interface). An off-site service like B2 or R2 also works.

Downloads always pass through the portal, which checks who's asking first, so the storage server never needs to be reachable from the internet. Every file remembers where it was saved, so files from before a switch keep working.

## Monthly reports

On the 1st of each month, the portal prepares a report for every active client covering the month before:

- **Requests:** how many were opened and resolved, how quickly you usually replied and fixed things, and what was fixed.
- **Ratings and comments,** on-site visits, and billing.
- **Equipment:** warranties ending in the next 3 months, if Snipe-IT is connected.

Under **Reports**, review each client's report, add a personal note, and send it. Owners get an email with a short summary and a link. A quiet month still gets a report, since "nothing broke" is what they pay you for.

By default you get an email on the 1st saying the reports are ready to review. Turn on **Send last month's reports automatically** to skip that step. Once sent, a report is saved exactly as it was, so later edits don't change what the client sees. **Send again** refreshes it.

The schedule runs from `/etc/cron.d/swyftech-portal-reports` at 13:00 UTC on the 1st, which is about 8 AM Central (`update` adds it to older installs). To run it by hand: `portal-cli monthly-reports --month 2026-09`.

Response times count from when a request arrived to your first reply the client could see, around the clock. Internal notes don't count.

## On-site visits

Owners and Billing people can book a visit from the open times you allow. They pick a date on a small calendar (only dates with openings can be chosen), then one of that day's open times, then add the details. Set them up under **Visits > Visit hours and calendar**:

- **Weekly hours:** the days and times you're willing to be on site.
- **Rules:** visit length (default 2 hours), how often a visit can start (every hour), the travel buffer kept free before and after each visit (30 minutes), minimum notice (24 hours), how far ahead clients can book (21 days), and the most visits in a day (2).
- **Confirmation:** by default, a booking shows as "Waiting for confirmation" until you confirm it, and you get an email for each one. Turn this off to let bookings confirm themselves.
- **Calendars to check:** paste the private ICS link of any calendar you live by (Google's "secret address in iCal format", Outlook's published calendar link, Nextcloud, or a work-schedule app's calendar feed). Busy times on those block bookings, recurring events included. Events marked "free" don't block. If a calendar can't be loaded, the page says so and bookings keep working without it, so keep confirmation on if that worries you.
- **Time off:** block whole days or a few hours.

From the **Visits** page you can confirm, move, cancel, add private notes (gate codes, parking), mark visits done, and schedule a visit yourself at any time. The portal warns you if it clashes with something but lets you go ahead. Clients are emailed whenever you confirm, move or cancel, and every visit has an "Add to my calendar" file.

To see visits in your own calendar app, copy the private link at the bottom of the visit hours page and subscribe to it in Google Calendar, Outlook or Apple Calendar. Calendar apps fetch it from their own servers, so the portal must be reachable over HTTPS. Anyone with the link can see client names and addresses; if it leaks, click **Make a new link**.

## Request ratings

When you resolve a request, the email to the person who opened it includes three links: Great, Okay and Not good. A link opens a page with that choice already selected and a Send button, plus an optional note. It doesn't save on its own, because Microsoft 365 and other email security scanners open every link in a message and would otherwise cast votes nobody made. Links work for 30 days without signing in.

In the portal, the person can rate a resolved request with one click and change their rating or add a note later. A "Not good" rating emails you right away. The Overview shows the share rated Great over the last 90 days and the latest "Not good" ratings, and each request shows its rating.

## Snipe-IT

The portal reads equipment from Snipe-IT and never writes back. Each portal client is linked to one Snipe-IT company.

1. In Snipe-IT, turn on **Settings > General > Full Multiple Companies Support** if you haven't, and create a company for each client. Put each client's assets in their company.
2. Create a user just for the portal (for example `portal-api`) with these permissions: **Assets: View**, **Users: View**, **Companies: View**, and permission to create its own API keys. Users: View matters: without it, Snipe-IT leaves out the email of the person a device is checked out to, so team members won't see their own devices.
3. Sign in to Snipe-IT as that user and create a token under **Manage API Keys**.
4. Add to `/opt/swyftech-portal/.env`, then `systemctl restart swyftech-portal`:

   ```
   SNIPEIT_URL=https://snipe.wolfe.house
   SNIPEIT_API_TOKEN=eyJ0eXAi...
   ```

   Use Snipe-IT's internal address. If it has a self-signed certificate, also set `SNIPEIT_VERIFY_TLS=false`.
5. Open each client in the portal and pick their Snipe-IT company under **Equipment**. The panel lists devices whose warranty ends within 60 days, which is a handy list for replacement quotes.

A few things to know:

- Team members are matched to devices by email, so the Snipe-IT user's email must match their portal email.
- Archived assets are hidden.
- The list refreshes every 5 minutes (`SNIPEIT_CACHE_MINUTES`).
- If Snipe-IT is down, clients see a short note on the Equipment page and everything else keeps working.
- If a company shows no devices even though Snipe-IT has some, the API user probably can't see that company. With Full Multiple Companies Support on, make sure the portal user isn't limited to a single company.

## First steps

1. Sign in with the staff login from the installer, set up your authenticator app, and save your backup codes.
2. Change your password under **Account**.
3. **Clients > Add a client**, with the owner's email. They get an invite (or you copy the link).
4. Upload your MSA under **Documents**, marked **Owner and Billing only**.
5. Write a few guides. Good first ones: resetting a Microsoft 365 password, spotting a phishing email, and sending a screenshot.

## Admin commands (inside the container)

```bash
portal-cli create-staff --email you@swyftech.net --name "Jacob"   # add --technician for a Technician
portal-cli reset-mfa --email person@client.com       # new phone, lost backup codes
portal-cli reset-password --email person@client.com
portal-cli list-users
portal-cli check-storage                              # test where files are stored
portal-cli move-files-to-s3                           # after switching to S3
portal-cli monthly-reports --month 2026-09            # prepare or send a month's reports
update                                                # pull the latest version and restart
journalctl -u swyftech-portal -f                      # live logs
```

You can also reset someone's authenticator from their client page. Confirm who they are by phone first.

## Backups

Everything that matters is in `/opt/swyftech-portal/data` (the database, plus uploaded files unless they're in S3) and `/opt/swyftech-portal/.env`. If you use S3, back up the bucket too, or turn on versioning in Garage, B2 or R2.

- A nightly job copies the database to `data/backups/`, keeping 14 days.
- `update` also backs up the database before changing anything.
- For real protection, include the container in a Proxmox Backup Server job so the uploads and settings are covered too.

## Security

- Every account requires an authenticator app. Codes can't be reused, and each person gets 8 single-use backup codes.
- Passwords are hashed with Argon2 and must be 12 or more characters.
- Five wrong tries pauses sign-in for that account for 15 minutes.
- Changing a password or turning someone off signs them out everywhere.
- Every form is protected against cross-site request forgery, and a strict Content-Security-Policy blocks inline and third-party scripts. Fonts and htmx are bundled, so the portal loads nothing from other sites.
- Sign-ins, invites, payments, downloads and admin actions are written to an audit log table.

## Project layout

```
app/
  main.py            app setup, security headers, error pages, Stripe webhook
  routes_auth.py     sign-in, authenticator setup, invites, password reset
  routes_client.py   client pages: home, requests, invoices, documents, guides, team, account
  routes_staff.py    SwyfTech pages: overview, clients, requests, invoices, documents, guides
  web.py             permissions, CSRF, rendering helpers
  db.py              SQLite schema and queries
  payments.py        Stripe Checkout and webhook
  security.py        passwords, authenticator codes, backup codes, link tokens
  mailer.py          SMTP
  snipeit.py         read-only Snipe-IT connection
  visits.py          visit hours, open times, safe booking, calendar import and export
  routes_visits.py   visit pages for clients and staff, private calendar feed
  feedback.py        request ratings and signed email links
  storage.py         local disk or S3-compatible storage for uploaded files
  attachments.py     files on requests: checks, saving, listing
  routes_files.py    permission-checked downloads
  reports.py         monthly report numbers, drafts and sending
  routes_reports.py  report pages for clients and staff
  routes_admin.py    Admin: business details, email, staff, sign-in and security
  overrides.py       settings saved in the portal, layered over .env
  cli.py             portal-cli
  templates/         Jinja2 pages
  static/            CSS, htmx, fonts (Chakra Petch, Atkinson Hyperlegible), logo
ct/swyftech-portal.sh               runs on the Proxmox host
install/swyftech-portal-install.sh  runs inside the container
```

## Running it locally

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp example.env .env      # set BASE_URL=http://localhost:8000
python -m app.cli create-staff --email you@example.com --name You
uvicorn app.main:app --reload
```
