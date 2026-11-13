"""Exceptions shared by the CLI and the session (kept in a module without heavy imports)."""


class SessionError(Exception):
    """A start-up problem the user can fix: no node found, several candidate ports."""
