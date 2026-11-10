"""Local setup and account management for the garage site.

    uv run python -m automotive.site_setup --setup-site
    uv run python -m automotive.site_setup --create-user NAME --role admin|employee
    uv run python -m automotive.site_setup --reset-password NAME
    uv run python -m automotive.site_setup --list-users
    uv run python -m automotive.site_setup --add-photos

``--setup-site`` only accepts a loopback DATABASE_URL (same rule as automotive.local_setup).
It applies migration 0012 alone, never another pending migration, creates one dedicated
login that is a member of ``garage_site`` only, and stores its URL in the private .env.

``--add-photos`` is for a site set up before a photo was packaged: it stores the packaged
gallery photos the gallery does not hold yet and leaves every stored photo as it is.
"""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sys
from pathlib import Path

import psycopg
from dotenv import dotenv_values, load_dotenv, set_key
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from automotive.local_setup import LocalSetupError, _require_catalog, local_admin_url
from automotive.site_auth import ROLES, USERNAME, hash_password, password_problem
from automotive.site_repository import Conflict, SiteRepository
from warehouse_sync.migrations import MIGRATIONS_DIRECTORY

ROOT = Path(__file__).resolve().parents[1]
SITE_MIGRATION = "0012_garage_site"
ENV_KEY = "AUTOMOTIVE_SITE_DATABASE_URL"


def _apply_site_migration(connection: psycopg.Connection) -> bool:
    """Apply 0012 exactly once and record it; other pending migrations are left alone."""
    with connection.transaction():
        if connection.execute(
            "SELECT 1 FROM public.warehouse_schema_migrations WHERE version = %s",
            (SITE_MIGRATION,),
        ).fetchone():
            return False
        path = MIGRATIONS_DIRECTORY / f"{SITE_MIGRATION}.up.sql"
        connection.execute(path.read_text(encoding="utf-8"))
        connection.execute(
            "INSERT INTO public.warehouse_schema_migrations (version) VALUES (%s)",
            (SITE_MIGRATION,))
        return True


def _new_login(connection: psycopg.Connection, admin_url: str) -> str:
    name = "garage_local_site_" + secrets.token_hex(6)
    password = secrets.token_urlsafe(48)
    verifier = connection.pgconn.encrypt_password(
        password.encode("ascii"), name.encode("ascii"), b"scram-sha-256").decode("ascii")
    connection.execute(sql.SQL(
        "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS "
        "INHERIT PASSWORD {}").format(sql.Identifier(name), sql.Literal(verifier)))
    connection.execute(sql.SQL("GRANT garage_site TO {}").format(sql.Identifier(name)))
    info = conninfo_to_dict(admin_url)
    return make_conninfo(**{
        key: info[key] for key in ("host", "hostaddr", "port", "dbname", "sslmode") if key in info
    }, user=name, password=password, connect_timeout=5)


def configure_site(env_path: Path | None = None) -> dict:
    """Provision the role, apply 0012, create the login once and seed default content."""
    env_path = env_path or ROOT / ".env"
    if not env_path.is_file():
        raise LocalSetupError("Copy .env.example to .env and configure local PostgreSQL first.")
    values = dotenv_values(env_path)
    admin_url = local_admin_url(values)
    existing = values.get(ENV_KEY)
    try:
        with psycopg.connect(admin_url) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(1728036741)")
            _require_catalog(connection)
            connection.execute((ROOT / "ops/sql/provision_automotive_roles.sql").read_text())
            applied = _apply_site_migration(connection)
            connection.execute("SELECT garage.provision()")
            created = None if existing else _new_login(connection, admin_url)
    except psycopg.Error:
        raise LocalSetupError(
            "Local PostgreSQL setup failed. Check DATABASE_URL, the running postgres service "
            "and local admin privileges. Credentials and server errors were withheld."
        ) from None
    if created:
        # The value never goes through stdout, shell arguments or application logs.
        os.chmod(env_path, 0o600)
        set_key(str(env_path), ENV_KEY, created)
    seeded = seed_defaults(SiteRepository(created or existing or ""))
    return {"migration_applied": applied, "login_created": bool(created), **seeded}


def seed_defaults(repository: SiteRepository) -> dict:
    """Store the packaged texts, services and gallery photos into still-empty tables."""
    from automotive.site_content import DEFAULT_SECTIONS, DEFAULT_SERVICES
    from automotive.site_media import packaged_gallery

    try:
        added = repository.seed(DEFAULT_SECTIONS, DEFAULT_SERVICES)
        photos = 0
        if not repository.load_content()["media"]:
            for photo in packaged_gallery():
                repository.add_media(**photo)
                photos += 1
    except psycopg.Error:
        raise LocalSetupError(
            "The site login could not store default content. Rerun --setup-site; "
            "credentials and server errors were withheld."
        ) from None
    return {"sections_seeded": added["sections"], "services_seeded": added["services"],
            "photos_seeded": photos}


def add_packaged_photos(repository: SiteRepository) -> dict:
    """Store the packaged gallery photos the gallery does not hold yet, after the others.

    Setup fills an empty gallery once; a photo packaged later reaches a site that is
    already set up only through this. A stored photo is never touched: its text, place
    and whether it is shown belong to the dashboard. One that was deleted there is stored
    again, which is why this is a command of its own and not part of every setup run.
    """
    from automotive.site_media import packaged_gallery

    added = shown = 0
    try:
        stored = {row["sha256"] for row in repository.load_content()["media"]}
        for photo in packaged_gallery():
            if photo["sha256"] in stored:
                continue
            repository.add_media(**photo)
            added += 1
            shown += bool(photo["published"])
        if added:
            repository.audit(None, "media.packaged_cli", "",
                             {"count": added, "published": shown})
    except psycopg.Error:
        raise LocalSetupError("The packaged photos could not be stored; database errors "
                              "were withheld.") from None
    return {"photos_added": added, "photos_published": shown}


def _repository() -> SiteRepository:
    load_dotenv(ROOT / ".env", override=False)
    repository = SiteRepository(os.getenv(ENV_KEY, ""))
    if not repository.configured:
        raise LocalSetupError(f"{ENV_KEY} is not set. Run --setup-site first.")
    return repository


def _ask_password(username: str, generate: bool) -> tuple[str, bool]:
    if generate:
        return secrets.token_urlsafe(15), True
    first = getpass.getpass(f"Password for {username} (at least 10 characters): ")
    if first != getpass.getpass("Repeat password: "):
        raise LocalSetupError("The passwords did not match; nothing was changed.")
    return first, False


def _checked(username: str, password: str) -> None:
    problem = password_problem(password, username)
    if problem:
        raise LocalSetupError(f"Password rejected ({problem}); nothing was changed.")


def create_user(username: str, role: str, display_name: str = "",
                generate: bool = False) -> str | None:
    """Create an account; returns the password only when it was generated here."""
    name = username.strip().lower()
    if not USERNAME.fullmatch(name):
        raise LocalSetupError("Username must be 3-32 characters: a-z, 0-9, dot, dash, "
                              "underscore, starting with a letter or digit.")
    if role not in ROLES:
        raise LocalSetupError("Role must be admin or employee.")
    repository = _repository()
    password, generated = _ask_password(name, generate)
    _checked(name, password)
    try:
        user = repository.create_user(name, display_name.strip()[:80], role,
                                      hash_password(password))
        repository.audit(None, "user.created_cli", name, {"role": role})
    except Conflict:
        raise LocalSetupError(f"An account named {name} already exists.") from None
    except psycopg.Error:
        raise LocalSetupError("The account could not be stored; database errors were "
                              "withheld.") from None
    del user
    return password if generated else None


def reset_password(username: str, generate: bool = False) -> str | None:
    """Set a new password, reactivating nothing, and end the account's sessions."""
    name = username.strip().lower()
    repository = _repository()
    try:
        row = repository.find_user(name)
        if not row:
            raise LocalSetupError(f"No account named {name}.")
        password, generated = _ask_password(name, generate)
        _checked(name, password)
        repository.set_password(row["user_id"], hash_password(password))
        repository.audit(None, "user.password_reset_cli", name)
    except psycopg.Error:
        raise LocalSetupError("The password could not be stored; database errors were "
                              "withheld.") from None
    return password if generated else None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Garage site setup and staff accounts")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--setup-site", action="store_true",
                        help="Apply migration 0012, create the local site login, seed content")
    action.add_argument("--create-user", metavar="NAME", help="Create a dashboard account")
    action.add_argument("--reset-password", metavar="NAME", help="Set a new password")
    action.add_argument("--list-users", action="store_true", help="List dashboard accounts")
    action.add_argument("--add-photos", action="store_true",
                        help="Store packaged gallery photos the gallery does not hold yet")
    parser.add_argument("--role", choices=ROLES, default="employee")
    parser.add_argument("--display-name", default="")
    parser.add_argument("--generate", action="store_true",
                        help="Generate the password and print it once instead of asking")
    args = parser.parse_args(argv)
    try:
        if args.setup_site:
            result = configure_site()
            print("Garage site database ready; the site login is saved in the private .env."
                  if result["login_created"] else
                  "Garage site database ready; the existing site login was kept.")
            print(f"Seeded {result['sections_seeded']} text sections, "
                  f"{result['services_seeded']} services, {result['photos_seeded']} photos.")
            print("Next: uv run python -m automotive.site_setup --create-user NAME --role admin")
        elif args.create_user:
            password = create_user(args.create_user, args.role, args.display_name,
                                   args.generate)
            print(f"Created {args.role} account {args.create_user.strip().lower()}.")
            if password:
                print(f"Generated password (shown once): {password}")
        elif args.reset_password:
            password = reset_password(args.reset_password, args.generate)
            print(f"Password changed for {args.reset_password.strip().lower()}; "
                  "its sessions were ended.")
            if password:
                print(f"Generated password (shown once): {password}")
        elif args.add_photos:
            result = add_packaged_photos(_repository())
            hidden = result["photos_added"] - result["photos_published"]
            print(f"Added {result['photos_added']} packaged photos to the gallery: "
                  f"{result['photos_published']} shown on the site, {hidden} not shown until "
                  "published in the dashboard." if result["photos_added"] else
                  "The gallery already holds every packaged photo; nothing was added.")
        else:
            for row in _repository().list_users():
                state = "active" if row["is_active"] else "disabled"
                print(f"{row['username']:<32} {row['role']:<9} {state}")
    except LocalSetupError as exc:
        parser.exit(2, f"{exc}\n")
    except psycopg.Error:
        parser.exit(2, "Database error; details were withheld.\n")


if __name__ == "__main__":
    main(sys.argv[1:])
