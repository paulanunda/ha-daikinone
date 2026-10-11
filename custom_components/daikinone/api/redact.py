"""Helpers for showing identifiers without revealing them."""

from __future__ import annotations


def mask_email(email: str) -> str:
    """Return an email with everything but its first character and domain suffix hidden."""
    local, sep, domain = email.partition("@")
    if not sep or not local:
        return "***"
    tld = domain.rpartition(".")[2] if "." in domain else ""
    return f"{local[0]}***@***.{tld}" if tld else f"{local[0]}***@***"


def mask_id(value: str) -> str:
    """Return an identifier reduced to its last four characters."""
    return f"***{value[-4:]}" if len(value) > 8 else "***"
