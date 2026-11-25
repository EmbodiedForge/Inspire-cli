"""Invocation-local Account Context selection and resolved credentials."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from inspire.config.models import Config


@dataclass(frozen=True)
class AccountContext:
    """Resolved account alias and platform authentication context."""

    alias: str | None
    username: str
    password: str = field(repr=False)
    base_url: str = ""
    auth_method: str = "http"

    @property
    def cache_key(self) -> str:
        """Return the local state key without conflating alias and username."""
        return self.alias or self.username


@dataclass(frozen=True)
class AccountOverride:
    alias: str
    source: str


_ACCOUNT_OVERRIDE: ContextVar[AccountOverride | None] = ContextVar(
    "inspire_account_override",
    default=None,
)


def set_account_override(alias: str, *, source: str = "cli") -> Token[AccountOverride | None]:
    value = str(alias or "").strip()
    if not value:
        raise ValueError("Account alias cannot be empty")
    return _ACCOUNT_OVERRIDE.set(AccountOverride(alias=value, source=source))


def reset_account_override(token: Token[AccountOverride | None]) -> None:
    _ACCOUNT_OVERRIDE.reset(token)


def get_account_override() -> AccountOverride | None:
    return _ACCOUNT_OVERRIDE.get()


def resolve_account_context(config: Config) -> AccountContext:
    """Build the authentication context from one effective runtime Config."""
    return AccountContext(
        alias=str(config.account_alias or "").strip() or None,
        username=str(config.username or "").strip(),
        password=config.password or "",
        base_url=str(config.base_url or "").rstrip("/"),
        auth_method=str(config.auth_method or "http").strip().lower(),
    )


__all__ = [
    "AccountContext",
    "AccountOverride",
    "get_account_override",
    "reset_account_override",
    "resolve_account_context",
    "set_account_override",
]
