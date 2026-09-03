"""Audit trail package.

Provides an append-only audit trail for the recovery lifecycle.

Each event captures what happened, why, and contains sanitized data
(never API keys, secrets, or full raw payloads).

Events can be retrieved chronologically for a given case_id.
"""
