"""Typed exceptions for the Aiper Irrisense 2 integration.

These let callers discriminate authentication failures from transient
connectivity problems without matching on exception *message* text (which was
brittle: a wording change silently reclassified a permanent auth error as a
retryable one). The API layer raises these; ``__init__`` maps them to Home
Assistant's ``ConfigEntryAuthFailed`` / ``ConfigEntryNotReady`` and the config
flow maps them to ``invalid_auth`` / ``cannot_connect`` form errors.
"""
from __future__ import annotations


class AiperError(Exception):
    """Base class for all Aiper API errors."""


class InvalidAuth(AiperError):
    """The Aiper cloud rejected the credentials (or returned no token).

    Permanent — the user must re-enter credentials. Maps to
    ``ConfigEntryAuthFailed`` / the ``invalid_auth`` form error.
    """


class CannotConnect(AiperError):
    """A transient failure reaching or talking to the Aiper cloud.

    Retryable — network blip, timeout, 5xx, unexpected protocol response.
    Maps to ``ConfigEntryNotReady`` / the ``cannot_connect`` form error.
    """
