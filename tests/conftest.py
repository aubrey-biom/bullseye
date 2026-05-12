"""Shared pytest fixtures and path setup."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make `src/` importable without an editable install.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

# Prevent .env in the user's home from leaking into tests.
os.environ.setdefault("BPD_DATA_DIR", str(ROOT / ".test-data"))
os.environ.setdefault("KITEWORKS_BASE_URL", "https://securesharek.target.com")
os.environ.setdefault("KITEWORKS_USERNAME", "test@example.com")
os.environ.setdefault("KITEWORKS_PASSWORD", "test-password")
