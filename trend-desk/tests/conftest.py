"""Puts scripts/ on the import path so tests import the desk modules by name."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
