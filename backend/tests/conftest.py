"""Test defaults: admin allowlist is empty unless configured, so tests that need an admin set one."""
import os

os.environ.setdefault("ADMIN_EMAILS", "admin@example.com")
