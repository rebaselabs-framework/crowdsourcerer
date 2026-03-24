"""Shared pytest configuration for the CrowdSorcerer API test suite.

Adds the project root (apps/api/) to sys.path so that test files can import
`main`, `core`, `models`, `routers`, and `workers` without needing an
installed package or a manual PYTHONPATH setting.
"""
import sys
import os
from pathlib import Path

# Add apps/api/ to sys.path (parent of the tests/ directory)
api_root = Path(__file__).parent.parent
if str(api_root) not in sys.path:
    sys.path.insert(0, str(api_root))
