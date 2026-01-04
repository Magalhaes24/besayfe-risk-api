"""
Helper to read human-friendly allergen labels from the generated allergens.csv.
Provides a cached lookup so other modules can surface readable category names.
"""

from __future__ import annotations

# Standard library helpers for CSV parsing, caching, and paths.
import csv
from functools import lru_cache
from pathlib import Path
from typing import Dict


def _labels_path() -> Path:
    """Location of the generated allergens.csv file."""
    # Keep the path derivation centralized for reuse and tests.
    return Path(__file__).resolve().parent.parent / "db" / "allergens.csv"


def _load_labels() -> Dict[str, str]:
    """Load allergen labels from CSV into a dict keyed by allergen code."""
    path = _labels_path()
    if not path.exists():
        return {}

    mapping: Dict[str, str] = {}
    # Parse the CSV and build a lowercase code -> label map.
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            code = (row.get("allergens_detected") or "").strip().lower()
            label = (row.get("allergen_categories") or "").strip()
            if not code or not label:
                continue
            # First seen wins to keep deterministic output
            mapping.setdefault(code, label)
    return mapping


@lru_cache(maxsize=1)
def _labels_cache() -> Dict[str, str]:
    # Cache the labels in memory to avoid repeated disk I/O.
    return _load_labels()


def allergen_label(code: str) -> str:
    """
    Return the human-friendly allergen category name from allergens.csv if available.
    Falls back to the code if no label is found.
    """
    if not code:
        return ""
    # Return a cached label if available; fall back to the raw code.
    label = _labels_cache().get(code.lower())
    return label or code
