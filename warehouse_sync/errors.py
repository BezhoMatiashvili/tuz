"""Domain-specific exceptions with safe, non-secret failure codes."""

import psycopg


class ConfigurationError(ValueError):
    """Raised for incomplete or unsafe local configuration."""


class AuthenticationError(RuntimeError):
    """Raised when the source no longer accepts the configured session."""


class SourceProtocolError(RuntimeError):
    """Raised when the source response cannot be parsed as an inventory page."""


class SourceChangedError(SourceProtocolError):
    """Raised when the source changes during an offset-paginated scan."""


def failure_code(error: BaseException) -> str:
    """Return a value which is safe to store, log, or include in an alert.

    Exception text can contain information emitted by a database driver or a
    third-party HTTP library. It must therefore never cross the service
    boundary into PostgreSQL, journald, or alert email.
    """
    if isinstance(error, AuthenticationError):
        return "authentication_rejected"
    if isinstance(error, ConfigurationError):
        return "configuration_error"
    if isinstance(error, SourceProtocolError):
        return "source_protocol_error"
    if isinstance(error, psycopg.Error):
        return "database_error"
    if isinstance(error, TimeoutError):
        return "timeout"
    return "sync_error"
