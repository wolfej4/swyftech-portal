"""Admin commands, run inside the container:

    portal-cli create-staff --email you@swyftech.net --name "Jacob"
    portal-cli reset-mfa --email someone@client.com
    portal-cli reset-password --email someone@example.com
    portal-cli list-users
    portal-cli check-storage
    portal-cli move-files-to-s3 [--keep-local]
    portal-cli monthly-reports [--month 2026-09]
"""
import argparse
import secrets
import sys

from . import db, security


def _password() -> str:
    words = secrets.token_urlsafe(18)
    return words


def create_staff(email: str, name: str, technician: bool = False) -> None:
    db.init()
    if db.one("SELECT 1 FROM users WHERE email = ?", (email,)):
        sys.exit(f"{email} already exists.")
    pw = _password()
    db.run("INSERT INTO users (client_id, email, name, role, password_hash, staff_admin, created_at) VALUES (NULL,?,?,'staff',?,?,?)",
           (email, name, security.hash_password(pw), 0 if technician else 1, db.now()))
    print(f"Staff account created ({'Technician' if technician else 'Admin'}).\n  Email:    {email}\n  Password: {pw}\n"
          "Sign in, then connect your authenticator app. Change the password under Account.")


def reset_mfa(email: str) -> None:
    n = db.run("UPDATE users SET totp_secret = NULL, totp_enabled = 0, backup_codes = '[]', "
               "session_version = session_version + 1 WHERE email = ?", (email,))
    print("Done. They'll set up a new authenticator app at next sign-in." if n else f"No user {email}.")


def reset_password(email: str) -> None:
    pw = _password()
    n = db.run("UPDATE users SET password_hash = ?, session_version = session_version + 1, failed_attempts = 0, "
               "locked_until = NULL WHERE email = ?", (security.hash_password(pw), email))
    print(f"New password for {email}: {pw}" if n else f"No user {email}.")


def list_users() -> None:
    for u in db.all("SELECT u.email, u.name, u.role, u.active, u.totp_enabled, c.name AS client "
                    "FROM users u LEFT JOIN clients c ON c.id = u.client_id ORDER BY c.name, u.role, u.email"):
        flags = ("" if u["active"] else " [disabled]") + ("" if u["totp_enabled"] else " [no MFA yet]")
        print(f"{(u['client'] or 'SwyfTech'):<28} {u['role']:<8} {u['email']:<36} {u['name']}{flags}")


def check_storage() -> None:
    from . import storage
    try:
        print(f"OK: files are stored on {storage.check()}.")
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Storage check failed: {exc}")


def move_files_to_s3(keep_local: bool) -> None:
    """Copy every file still on local disk into S3, verify it, then point the record at S3."""
    import tempfile
    from . import config, storage
    if not config.s3_enabled():
        sys.exit("Set S3_BUCKET, S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY in .env first.")
    storage.check()
    moved = failed = 0
    jobs = [("documents", r["id"], r["stored_name"], r["filename"])
            for r in db.all("SELECT id, stored_name, filename FROM documents WHERE storage = 'local'")]
    jobs += [("attachments", r["id"], r["storage_key"], r["filename"])
             for r in db.all("SELECT id, storage_key, filename FROM attachments WHERE storage = 'local'")]
    for table, row_id, key, filename in jobs:
        new_key = key if "/" in key else f"documents/{key}"  # very old documents sat at the top of uploads/
        try:
            chunks, size = storage.open_stream("local", key)
            with tempfile.TemporaryFile() as tmp:
                for c in chunks:
                    tmp.write(c)
                storage.save(tmp, new_key, storage.content_type_for(filename))
            check_chunks, check_size = storage.open_stream("s3", new_key)
            for _ in check_chunks:
                pass
            if check_size != size:
                raise storage.StorageError(f"size mismatch ({check_size} vs {size})")
            col = "stored_name" if table == "documents" else "storage_key"
            db.run(f"UPDATE {table} SET storage = 's3', {col} = ? WHERE id = ?", (new_key, row_id))
            if not keep_local:
                storage.delete("local", key)
            moved += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  Skipped {filename}: {exc}")
    print(f"Moved {moved} file(s) to S3." + (f" {failed} failed; run again to retry." if failed else ""))


def monthly_reports(month: str | None) -> None:
    """Run from cron on the 1st: send last month's reports, or tell staff they're ready to review."""
    import time
    from . import config, mailer, reports
    month = month or reports.last_month()
    if not reports.valid_month(month):
        sys.exit("Month must look like 2026-09.")
    clients = reports.active_clients()
    if reports.auto_send_enabled():
        sent = 0
        for c in clients:
            rec = reports.record(c["id"], month)
            if not (rec and rec["sent_at"]):
                reports.send(c["id"], month, None)
                sent += 1
        summary = f"Sent {sent} {reports.label(month)} report(s) automatically."
    else:
        summary = f"{len(clients)} {reports.label(month)} report(s) are ready to review."
    if config.STAFF_NOTIFY_EMAIL:
        mailer.send(config.STAFF_NOTIFY_EMAIL, f"Monthly reports: {reports.label(month)}",
                    f"{summary}\n\n{config.BASE_URL}/staff/reports?month={month}")
    if config.smtp_enabled():
        time.sleep(5)  # emails go out on background threads; let them finish before the command exits
    print(summary)


def main() -> None:
    parser = argparse.ArgumentParser(prog="portal-cli", description="SwyfTech portal admin commands")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("create-staff", help="Create a SwyfTech staff login")
    p.add_argument("--email", required=True)
    p.add_argument("--name", default="")
    p.add_argument("--technician", action="store_true", help="No access to billing, reports or Admin settings")
    for cmd in ("reset-mfa", "reset-password"):
        sub.add_parser(cmd).add_argument("--email", required=True)
    sub.add_parser("list-users")
    sub.add_parser("check-storage", help="Write, read and delete a test file where uploads are stored")
    mv = sub.add_parser("move-files-to-s3", help="Move files saved on local disk into S3")
    mv.add_argument("--keep-local", action="store_true", help="Leave the local copies in place")
    mr = sub.add_parser("monthly-reports", help="Send or announce last month's client reports (run on the 1st)")
    mr.add_argument("--month", default=None)
    args = parser.parse_args()
    db.init()
    {"create-staff": lambda: create_staff(args.email, args.name, args.technician),
     "reset-mfa": lambda: reset_mfa(args.email),
     "reset-password": lambda: reset_password(args.email),
     "list-users": list_users,
     "check-storage": check_storage,
     "move-files-to-s3": lambda: move_files_to_s3(args.keep_local),
     "monthly-reports": lambda: monthly_reports(args.month)}[args.cmd]()


if __name__ == "__main__":
    main()
