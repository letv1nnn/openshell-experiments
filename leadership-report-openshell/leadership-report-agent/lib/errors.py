"""Shared error types and exit codes for the leadership report agent."""

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_PERMANENT = 2
EXIT_TRANSIENT = 3


class AgentError(Exception):
    """Base exception for agent pipeline failures."""

    def __init__(self, message: str, retriable: bool = False):
        super().__init__(message)
        self.retriable = retriable


class AuthError(AgentError):
    """Raised when Google OAuth2 authentication fails."""
    pass
