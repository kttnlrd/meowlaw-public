#!/usr/bin/env python3
"""Rebuild search_index.db without starting the server."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.services.search_service import init_search_index

if __name__ == "__main__":
    print("Rebuilding search index...")
    init_search_index()
    print("Done.")
