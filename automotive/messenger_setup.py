"""Local setup and checks for Garage on Facebook (Messenger bot, comments, page stats, ads).

    uv run python -m automotive.messenger_setup --setup
    uv run python -m automotive.messenger_setup --status
    uv run python -m automotive.messenger_setup --connect [--page-id ID]
    uv run python -m automotive.messenger_setup --probe [--psid N]
    uv run python -m automotive.messenger_setup --discover-ads
    uv run python -m automotive.messenger_setup --events [N]
    uv run python -m automotive.messenger_setup --check-public

``--setup`` only accepts a loopback DATABASE_URL (the automotive.local_setup rule) and a database
where the garage site (migration 0012) is already set up. It applies the Messenger migrations
(0013, 0014) that are missing, never another pending one, and creates, when .env has none, one
login for the public receiver (a member of garage_webhook only) and one for the worker
(garage_messenger only). It also generates the webhook verify token and the token encryption
key. It prints the names of the keys it added, never a value, and a rerun changes nothing that is
already there (it applies a Messenger migration added since).

The other commands act as the dashboard does, with the site login (AUTOMOTIVE_SITE_DATABASE_URL):
connecting and disconnecting the page are dashboard rights, and the worker's login cannot store
a token. ``--connect`` reads the pasted token with getpass, so it never reaches the shell history
or the process list.
"""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
import requests
from dotenv import dotenv_values, load_dotenv, set_key
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from automotive.local_setup import LocalSetupError, _require_catalog, local_admin_url
from automotive.meta_config import DEFAULT_GRAPH_VERSION, MetaConfigError, MetaSettings
from automotive.meta_connect import ConnectError, connect, discover_ads, probe
from automotive.meta_context import MetaContext, NotConnected, build_context
from automotive.meta_crypto import TokenCipherError, new_key_hex
from automotive.meta_graph import GraphError
from automotive.meta_store import bot_mode, bot_state
from warehouse_sync.migrations import MIGRATIONS_DIRECTORY

ROOT = Path(__file__).resolve().parents[1]
SITE_MIGRATION = "0012_garage_site"
MESSENGER_MIGRATION = "0013_meta_messenger"
# Garage on Facebook's own migrations, in order: 0014 makes a customer's data deletion reach
# their comments (meta_store.forget_comments). A rerun of --setup applies what is missing.
MESSENGER_MIGRATIONS = (MESSENGER_MIGRATION, "0014_meta_forget")
SITE_ENV_KEY = "AUTOMOTIVE_SITE_DATABASE_URL"
RECEIVER_ENV_KEY = "MESSENGER_RECEIVER_DATABASE_URL"
WORKER_ENV_KEY = "MESSENGER_DATABASE_URL"
# (.env key, capability role, login name prefix): each login is a member of one role only.
LOGINS = ((RECEIVER_ENV_KEY, "garage_webhook", "garage_local_webhook_"),
          (WORKER_ENV_KEY, "garage_messenger", "garage_local_messenger_"))
# TNLU's reserved ngrok address, idle since TNLU moved to its server (see the plan).
DEFAULT_PUBLIC_URL = "https://glorified-stratus-budding.ngrok-free.dev"
ADVISORY_LOCK = 1728036741  # the same key as site_setup and local_setup: one setup at a time


def _applied(connection: psycopg.Connection, version: str) -> bool:
    return connection.execute("SELECT 1 FROM public.warehouse_schema_migrations "
                              "WHERE version = %s", (version,)).fetchone() is not None


def _apply_messenger_migration(connection: psycopg.Connection) -> bool:
    """Apply each of MESSENGER_MIGRATIONS exactly once, in order, and record it; other pending
    migrations are left alone. True when any was applied now."""
    applied = False
    for version in MESSENGER_MIGRATIONS:
        with connection.transaction():
            if _applied(connection, version):
                continue
            path = MIGRATIONS_DIRECTORY / f"{version}.up.sql"
            connection.execute(path.read_text(encoding="utf-8"))
            connection.execute("INSERT INTO public.warehouse_schema_migrations (version) "
                               "VALUES (%s)", (version,))
            applied = True
    return applied


def _new_login(connection: psycopg.Connection, admin_url: str, capability: str,
               prefix: str) -> str:
    """A LOGIN role in ``capability`` only; the server sees a SCRAM verifier, not the password."""
    name = prefix + secrets.token_hex(6)
    password = secrets.token_urlsafe(48)
    verifier = connection.pgconn.encrypt_password(
        password.encode("ascii"), name.encode("ascii"), b"scram-sha-256").decode("ascii")
    connection.execute(sql.SQL(
        "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS "
        "INHERIT PASSWORD {}").format(sql.Identifier(name), sql.Literal(verifier)))
    connection.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(capability),
                                                        sql.Identifier(name)))
    info = conninfo_to_dict(admin_url)
    return make_conninfo(**{
        key: info[key] for key in ("host", "hostaddr", "port", "dbname", "sslmode") if key in info
    }, user=name, password=password, connect_timeout=5)


def configure_messenger(env_path: Path | None = None) -> dict:
    """Provision the roles, apply 0013 and 0014, create the missing logins and generated keys.

    Returns migration_applied, logins_created (.env keys), keys_added (every .env key written)
    and still_needed (keys only the owner can fill in).
    """
    env_path = env_path or ROOT / ".env"
    if not env_path.is_file():
        raise LocalSetupError("Copy .env.example to .env and configure local PostgreSQL first.")
    values = dotenv_values(env_path)
    admin_url = local_admin_url(values)
    wanted = [login for login in LOGINS if not values.get(login[0])]
    try:
        with psycopg.connect(admin_url) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK,))
            _require_catalog(connection)
            if not _applied(connection, SITE_MIGRATION):
                raise LocalSetupError(
                    "The garage site database is not set up yet. Run `uv run python -m "
                    "automotive.site_setup --setup-site` first, then rerun --setup.")
            connection.execute((ROOT / "ops/sql/provision_automotive_roles.sql").read_text(
                encoding="utf-8"))
            applied = _apply_messenger_migration(connection)
            connection.execute("SELECT garage.meta_provision()")
            created = {key: _new_login(connection, admin_url, capability, prefix)
                       for key, capability, prefix in wanted}
    except psycopg.Error:
        raise LocalSetupError(
            "Local PostgreSQL setup failed. Check DATABASE_URL, the running postgres service "
            "and local admin privileges. Credentials and server errors were withheld."
        ) from None
    additions: dict[str, str] = dict(created)
    for key, make in (("META_WEBHOOK_VERIFY_TOKEN", lambda: secrets.token_urlsafe(48)),
                      ("META_TOKEN_ENCRYPTION_KEY", new_key_hex),
                      ("MESSENGER_PUBLIC_URL", lambda: DEFAULT_PUBLIC_URL),
                      ("META_GRAPH_API_VERSION", lambda: DEFAULT_GRAPH_VERSION)):
        if not values.get(key):
            additions[key] = make()
    # Private before anything secret is written; values never go through stdout or logs.
    os.chmod(env_path, 0o600)
    for key, value in additions.items():
        set_key(str(env_path), key, value)
    return {"migration_applied": applied, "logins_created": list(created),
            "keys_added": list(additions),
            "still_needed": [key for key in ("META_APP_ID", "META_APP_SECRET")
                             if not values.get(key)]}


# -- commands that act like the dashboard ------------------------------------------------------

def _context() -> MetaContext:
    load_dotenv(ROOT / ".env", override=False)
    url = os.getenv(SITE_ENV_KEY, "")
    if not url:
        raise LocalSetupError(f"{SITE_ENV_KEY} is not set. Run `uv run python -m "
                              "automotive.site_setup --setup-site` first.")
    return build_context(MetaSettings.from_env(), url, application_name="garage-messenger-setup")


def _time(value: datetime | None, missing: str = "-") -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M %Z") if value else missing


def status_lines(ctx: MetaContext) -> list[str]:
    """The connection, token health (never a token), switches, last webhook, today's counts."""
    store = ctx.store
    page = store.page()
    if page:
        lines = [f"Page: {page['page_name'] or '(no name)'} (id {page['page_id']}), connected "
                 f"{_time(page['connected_at'])} by {page['connected_by'] or 'unknown'}",
                 "Webhook fields: " + (", ".join(page["subscribed_fields"]) or "none") + (
                     f" (since {_time(page['subscribed_at'])})" if page["subscribed_at"] else "")]
    else:
        lines = ["Page: not connected. Use --connect or Dashboard -> Facebook -> Connect."]
    for row in store.token_rows():
        detail = f" ({row['status_detail']})" if row["status_detail"] else ""
        lines.append(f"Token {row['purpose']:<9} {row['status']:<6} app {row['app_id']}, "
                     f"expires {_time(row['expires_at'], 'never')}, data access until "
                     f"{_time(row['data_access_expires_at'], 'unknown')}{detail}")
    settings = store.settings()
    state = bot_state(settings)
    mode = bot_mode(settings)
    since = (f" since {_time(settings.get('bot_test_at'))}" if mode == "test"
             else f" since {_time(settings['bot_enabled_at'])}" if mode == "on" else "")
    if mode == "test":
        since += f" (testers only: {len(store.testers())})"
    lines.append(f"Messenger bot: {state.upper()}{since}; Meta auto reply confirmed off: "
                 f"{'yes' if settings['auto_reply_confirmed_off'] else 'no'}")
    lines.append(f"Comment replies: {'ON' if settings['comments_enabled'] else 'OFF'}")
    last = store.last_event_at()
    lines.append(f"Last webhook: {_time(last['received_at'])} ({last['field']})" if last
                 else "Last webhook: none received yet")
    counts = store.counts_today()
    lines.append(f"Today: {counts['webhooks']} webhooks, {counts['customer_messages']} customer "
                 f"messages, {counts['replies_sent']} replies sent, {counts['fallbacks']} "
                 f"fallbacks, {counts['failed']} failed, {counts['turns']} engine turns, "
                 f"${counts['cost_usd']:.2f}, {counts['attention']} chats need attention")
    return lines


def check_public(settings: MetaSettings, *, session: Any = None) -> list[dict]:
    """From outside, through the tunnel: a wrong verify token must get 403 (and never the
    challenge back), and /healthz must answer 200. The real verify token is never sent."""
    if not settings.public_url:
        raise LocalSetupError("MESSENGER_PUBLIC_URL is not set; run --setup first.")
    http = session if session is not None else requests.Session()
    # ngrok's free plan shows browsers a warning page; this header skips it, as Meta's servers do.
    headers = {"ngrok-skip-browser-warning": "1", "User-Agent": "garage-1886-check/1.0"}
    challenge = secrets.token_hex(8)
    checks = (("webhook refuses a wrong verify token", settings.webhook_url,
               {"hub.mode": "subscribe", "hub.verify_token": secrets.token_urlsafe(32),
                "hub.challenge": challenge}, 403),
              ("health check", settings.public_url + "/healthz", None, 200))
    results = []
    for name, url, params, expected in checks:
        status = None
        leaked = False
        try:
            response = http.get(url, params=params, headers=headers, timeout=(5, 10),
                                allow_redirects=False)
            status = response.status_code
            leaked = challenge in (response.text or "")
        except requests.RequestException:
            pass
        results.append({"check": name, "status": status, "expected": expected,
                        "ok": status == expected and not leaked})
    return results


def _print_connect(result: dict) -> None:
    print(f"Connected {result['page_name'] or '(no name)'} (id {result['page_id']}).")
    print(f"Login token expires {_time(result['user_expires_at'], 'never')}; data access until "
          f"{_time(result['data_access_expires_at'], 'unknown')}; page token expires "
          f"{_time(result['page_expires_at'], 'never')}.")
    if result["missing_scopes"]:
        print("Missing permissions (those features stay off until a new paste): "
              + ", ".join(result["missing_scopes"]))
    subscription = result["subscription"]
    if subscription["ok"]:
        print("Webhooks subscribed (app and page).")
    else:
        error = subscription["error"] or {}
        meta = error.get("meta") or {}
        print(f"Webhook subscription failed at the {error.get('step', '?')} step (Meta code "
              f"{meta.get('code')}: {meta.get('message') or meta.get('type')}). The connection "
              "is kept; make sure the receiver and the tunnel run, then rerun --connect.")


def _connect_refused(error: ConnectError) -> str:
    detail = error.detail
    lines = [f"Connect refused: {error.code}."]
    if detail.get("missing"):
        lines.append("Missing: " + ", ".join(map(str, detail["missing"])))
    if detail.get("choices"):
        lines.append("Pages this login manages: " + "; ".join(
            f"{choice['name']} (id {choice['id']})" for choice in detail["choices"]))
        lines.append("Rerun with --page-id ID, or set META_PAGE_ID in .env.")
    meta = detail.get("meta")
    if meta:
        lines.append(f"Meta answered code {meta.get('code')}/{meta.get('subcode')}: "
                     f"{meta.get('message') or meta.get('type')}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Garage on Facebook: setup and checks")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--setup", action="store_true",
                        help="Apply migrations 0013 and 0014, create the receiver and worker "
                             "logins")
    action.add_argument("--status", action="store_true", help="Connection, switches, today")
    action.add_argument("--connect", action="store_true",
                        help="Connect the page with a pasted Graph API Explorer user token")
    action.add_argument("--probe", action="store_true",
                        help="Ask Meta whether the bot may message customers (sends nothing)")
    action.add_argument("--discover-ads", action="store_true",
                        help="Count the ads promoting the page in every readable ad account")
    action.add_argument("--events", nargs="?", const=20, type=int, metavar="N",
                        help="The newest webhook events (no message text)")
    action.add_argument("--check-public", action="store_true",
                        help="Check the public receiver through the tunnel")
    parser.add_argument("--psid", help="With --probe: the customer to probe")
    parser.add_argument("--page-id", help="With --connect: the page, when the login has several")
    args = parser.parse_args(argv)
    try:
        if args.setup:
            result = configure_messenger()
            print("Messenger database ready; migrations " + ", ".join(MESSENGER_MIGRATIONS)
                  + (" applied now." if result["migration_applied"]
                     else " were already applied."))
            print("Added to the private .env: " + (", ".join(result["keys_added"]) or "nothing"))
            if result["still_needed"]:
                print("Still needed in .env (from the GARAGE 1886 app): "
                      + ", ".join(result["still_needed"]))
            print("Next: start the receiver and the tunnel, then connect the page "
                  "(--connect or Dashboard -> Facebook).")
        elif args.check_public:
            results = check_public(MetaSettings.from_env())
            for item in results:
                print(f"{'ok  ' if item['ok'] else 'FAIL'} {item['check']}: HTTP "
                      f"{item['status'] if item['status'] is not None else 'no answer'} "
                      f"(expected {item['expected']})")
            if not all(item["ok"] for item in results):
                parser.exit(1)
        else:
            ctx = _context()
            if args.status:
                print("\n".join(status_lines(ctx)))
            elif args.connect:
                token = getpass.getpass("Paste the user token from Graph API Explorer "
                                        "(input is hidden): ")
                _print_connect(connect(ctx, token, actor="cli", page_id=args.page_id))
            elif args.probe:
                result = probe(ctx, args.psid, actor="cli")
                print(f"Probe: {result['result']} ({result['reason'] or 'no reason given'}); "
                      f"Meta code {result['code']}/{result['subcode']}; customer "
                      f"{result['psid'] or '-'}; token {result['send_purpose']}; "
                      f"tried {result['tried']}.")
            elif args.discover_ads:
                accounts = discover_ads(ctx)
                for account in accounts:
                    problem = (f" (unreadable: code {account['error']['code']})"
                               if account["error"] else "")
                    print(f"act_{account['account_id']} {account['name']} "
                          f"[{account['currency'] or '-'}]: {account['garage_ads']} of "
                          f"{account['ads_scanned']} ads promote the page{problem}")
                if not accounts:
                    print("This login can read no ad account.")
            else:
                for row in ctx.store.recent_events(max(1, min(args.events, 200))):
                    print(f"#{row['inbox_id']} {row['field']:<16} received "
                          f"{_time(row['received_at'])}, processed "
                          f"{_time(row['processed_at'], 'pending')}, attempts "
                          f"{row['attempts']}, {row['outcome'] or '-'}")
    except LocalSetupError as exc:
        parser.exit(2, f"{exc}\n")
    except ConnectError as exc:
        parser.exit(2, _connect_refused(exc))
    except NotConnected as exc:
        parser.exit(2, f"{exc}.\n")
    except GraphError as exc:
        parser.exit(2, f"Meta refused the request ({exc}).\n")
    except (MetaConfigError, TokenCipherError) as exc:
        parser.exit(2, f"{exc}\n")
    except psycopg.Error:
        parser.exit(2, "Database error; details were withheld.\n")


if __name__ == "__main__":
    main(sys.argv[1:])
