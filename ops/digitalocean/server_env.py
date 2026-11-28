"""Split the laptop's .env into one settings file per server service.

On the laptop every process reads the whole private .env. On the server each service runs
as its own account and gets a file with only the settings it uses, so the public webhook
receiver cannot read the AI keys or another service's database login. Every database login
also gets a fresh password, so no laptop password is sent to the server.

A setting this module has no rule for stops the run. A new key is then never dropped without
anyone noticing, and never handed to a service that should not hold it.

Used by ops/digitalocean/push-env. See docs/deploy-digitalocean.md.
"""

from __future__ import annotations

import argparse
import io
import re
import secrets
import sys
import tarfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

ADMIN = "admin.env"
SITE = "site.env"
MESSENGER = "messenger.env"
RECEIVER = "receiver.env"
SYNC = "sync.env"
INDEX = "index.env"
CRAWLER = "crawler.env"
FILES = (ADMIN, SITE, MESSENGER, RECEIVER, SYNC, INDEX, CRAWLER)
ROLES_SQL = "roles.sql"

# The server's PostgreSQL listens where the laptop's does (ops/digitalocean/server/
# postgres.compose.yaml), so a connection string only needs a new password.
DATABASE_HOSTS = frozenset({"127.0.0.1", "localhost"})
DATABASE_PORT = 55432
# nginx on the server connects to the site from this address (server/nginx/).
TRUSTED_PROXY = "127.0.0.2"

_AI_KEYS = frozenset({"ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "CATALOG_API_KEY"})
_RECEIVER_META = frozenset(
    {"META_APP_SECRET", "META_WEBHOOK_VERIFY_TOKEN", "META_GRAPH_API_VERSION"})
_ROLE = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_PASSWORD = re.compile(r"[A-Za-z0-9_-]{32,}")
_KEYWORD_USER = re.compile(r"(?:^|\s)user=(\S+)")
_KEYWORD_PASSWORD = re.compile(r"(^|\s)password=\S+")
_KEYWORD_HOST = re.compile(r"(?:^|\s)host=(\S+)")
_KEYWORD_PORT = re.compile(r"(?:^|\s)port=(\S+)")


class EnvError(ValueError):
    """The .env cannot be split safely. The message never contains a value."""


@dataclass(frozen=True)
class Plan:
    """What goes to the server: settings per file, and a new password per database login."""

    files: dict[str, dict[str, str]]
    passwords: dict[str, str]


def destinations(key: str) -> tuple[str, ...]:
    """The server files a laptop setting belongs in; () for laptop-only settings."""
    if key.startswith("POSTGRES_"):
        return ()  # the container's own settings: the server makes its own
    if key == "DATABASE_URL":
        return (ADMIN, SYNC)
    if key.startswith("WAREHOUSE_"):
        return (SYNC,)
    if key.startswith("PARTS_CRAWLER_"):
        return (CRAWLER,)
    if key in {"MESSENGER_RECEIVER_DATABASE_URL", "MESSENGER_RECEIVER_PORT"}:
        return (RECEIVER,)
    if key in _RECEIVER_META:
        return (SITE, MESSENGER, RECEIVER)
    if key.startswith("META_") or key == "MESSENGER_PUBLIC_URL":
        return (SITE, MESSENGER)
    if key.startswith("MESSENGER_"):
        return (MESSENGER,)
    if key == "AUTOMOTIVE_ENRICHMENT_DATABASE_URL":
        return (SITE, MESSENGER, INDEX)
    if key.startswith("AUTOMOTIVE_") or key in _AI_KEYS or key.endswith("_ENABLED"):
        return (SITE, MESSENGER)
    raise EnvError(f"no rule for the setting {key}: add one to ops/digitalocean/server_env.py")


def _is_database_url(key: str) -> bool:
    return key.endswith("DATABASE_URL")


def database_login(key: str, value: str) -> str:
    """The role a connection string signs in as. Accepts a URL or libpq keywords."""
    if "://" in value:
        parts = urlsplit(value)
        user, host, port = parts.username, parts.hostname, parts.port
    else:
        found = {name: pattern.search(value) for name, pattern in (
            ("user", _KEYWORD_USER), ("host", _KEYWORD_HOST), ("port", _KEYWORD_PORT))}
        user = found["user"].group(1) if found["user"] else None
        host = found["host"].group(1) if found["host"] else None
        port_text = found["port"].group(1) if found["port"] else ""
        port = int(port_text) if port_text.isdigit() else None
        if not _KEYWORD_PASSWORD.search(value):
            raise EnvError(f"{key} has no password to replace")
    if not user or not _ROLE.fullmatch(user):
        raise EnvError(f"{key} does not name a plain database role")
    if host not in DATABASE_HOSTS or port != DATABASE_PORT:
        raise EnvError(f"{key} must point at 127.0.0.1:{DATABASE_PORT}, as on the server")
    return user


def with_password(value: str, password: str) -> str:
    """The same connection string with another password."""
    if not _PASSWORD.fullmatch(password):
        raise EnvError("a generated password has an unexpected shape")
    if "://" in value:
        parts = urlsplit(value)
        host = parts.hostname or ""
        netloc = f"{quote(parts.username or '', safe='')}:{password}@{host}"
        if parts.port:
            netloc += f":{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return _KEYWORD_PASSWORD.sub(lambda match: f"{match.group(1)}password={password}", value)


def build(env: Mapping[str, str | None], *, domain: str, rehearsal: bool = False,
          warehouse_relay_port: int | None = None,
          new_password: Callable[[], str] = lambda: secrets.token_urlsafe(48)) -> Plan:
    """Split ``env`` (the parsed laptop .env) for the server.

    ``rehearsal`` keeps the laptop's public addresses, for a test run before the domain
    points at the server: the site is then reached through an SSH tunnel on loopback.

    ``warehouse_relay_port`` makes the server's sync read the warehouse on that loopback
    port, where ops/digitalocean/warehouse-relay forwards it through the laptop. The
    warehouse's own address then never reaches the server.
    """
    if warehouse_relay_port is not None and not 1024 <= warehouse_relay_port <= 65535:
        raise EnvError("the warehouse relay port must be between 1024 and 65535")
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+", domain):
        raise EnvError("the domain must be a plain host name such as example.ge")
    files: dict[str, dict[str, str]] = {name: {} for name in FILES}
    passwords: dict[str, str] = {}
    for key, raw in env.items():
        value = (raw or "").strip()
        if not value:
            continue
        if "\n" in value or "\r" in value or "\x00" in value:
            raise EnvError(f"{key} holds a line break, which a settings file cannot carry")
        targets = destinations(key)
        if not targets:
            continue
        if _is_database_url(key):
            login = database_login(key, value)
            value = with_password(value, passwords.setdefault(login, new_password()))
        for name in targets:
            files[name][key] = value
    for name in (ADMIN, SYNC):
        if "DATABASE_URL" not in files[name]:
            raise EnvError("DATABASE_URL is missing from .env")
    if warehouse_relay_port is not None:
        files[SYNC]["WAREHOUSE_BASE_URL"] = f"http://127.0.0.1:{warehouse_relay_port}"
    if not rehearsal:
        origin = f"https://{domain}"
        files[SITE]["AUTOMOTIVE_PUBLIC_ORIGINS"] = origin
        files[SITE]["AUTOMOTIVE_TRUSTED_PROXIES"] = TRUSTED_PROXY
        for name in (SITE, MESSENGER):
            files[name]["MESSENGER_PUBLIC_URL"] = origin
    return Plan(files=files, passwords=passwords)


def render_value(value: str) -> str:
    """One value in systemd's EnvironmentFile syntax (see systemd.exec(5))."""
    if "'" not in value:
        return f"'{value}'"  # single quotes: everything inside is literal
    escaped = value
    for character in ("\\", '"', "$", "`"):
        escaped = escaped.replace(character, "\\" + character)
    return f'"{escaped}"'


def render_env(settings: Mapping[str, str]) -> str:
    return "".join(f"{key}={render_value(settings[key])}\n" for key in sorted(settings))


def render_roles_sql(passwords: Mapping[str, str]) -> str:
    """SQL that gives every login its new password, or changes nothing if a role is missing.

    The two SET lines keep the statements, and so the passwords, out of the server log.
    """
    for role, password in passwords.items():
        if not _ROLE.fullmatch(role) or not _PASSWORD.fullmatch(password):
            raise EnvError("a role name or password has an unexpected shape")
    names = ", ".join(f"'{role}'" for role in sorted(passwords))
    lines = [
        "SET log_statement = 'none';",
        "SET log_min_error_statement = 'panic';",
        "BEGIN;",
        "DO $$",
        "DECLARE missing text;",
        "BEGIN",
        f"    SELECT string_agg(name, ', ') INTO missing FROM unnest(ARRAY[{names}]) AS name",
        "    WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = name);",
        "    IF missing IS NOT NULL THEN",
        "        RAISE EXCEPTION 'database roles missing: % (run copy-database first)', missing;",
        "    END IF;",
        "END $$;",
    ]
    lines += [f"ALTER ROLE \"{role}\" PASSWORD '{passwords[role]}';" for role in sorted(passwords)]
    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def to_tar(plan: Plan) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        members = {name: render_env(plan.files[name]) for name in FILES}
        members[ROLES_SQL] = render_roles_sql(plan.passwords)
        for name, text in members.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def describe(plan: Plan) -> str:
    """Which setting goes where. Names only, never a value."""
    lines = [f"{name} ({len(plan.files[name])}): {', '.join(sorted(plan.files[name]))}"
             for name in FILES]
    lines.append(f"new database passwords for: {', '.join(sorted(plan.passwords))}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", required=True, help="path of the laptop's .env")
    parser.add_argument("--domain", required=True)
    parser.add_argument("--rehearsal", action="store_true")
    parser.add_argument("--warehouse-relay-port", type=int, default=None,
                        help="loopback port on the server that leads to the warehouse")
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument("--plan", action="store_true", help="print setting names per file")
    output.add_argument("--tar", action="store_true", help="write the files as a tar stream")
    args = parser.parse_args(argv)

    from dotenv import dotenv_values

    try:
        plan = build(dotenv_values(args.env), domain=args.domain, rehearsal=args.rehearsal,
                     warehouse_relay_port=args.warehouse_relay_port)
    except EnvError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.plan:
        sys.stdout.write(describe(plan))
        return 0
    if sys.stdout.isatty():
        print("error: --tar writes secrets; pipe it to the server, not a terminal",
              file=sys.stderr)
        return 2
    sys.stdout.buffer.write(to_tar(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
