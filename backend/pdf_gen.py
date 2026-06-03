"""Thin wrapper around the flip-board PDF generator.

Loads the existing PDF generation logic from the user's skill folder
(or a vendored copy) and exposes simple build_deal_pdf / build_comparison_pdf.
"""

import importlib.util
import os
import sys
from pathlib import Path

# Locate the canonical PDF generator from the user's skill.
_SKILL_PDF = Path.home() / ".claude" / "skills" / "flip-board" / "scripts" / "generate_flip_board_pdf.py"


def _load():
    if not _SKILL_PDF.exists():
        raise RuntimeError(
            f"PDF generator not found at {_SKILL_PDF}. "
            "Install the flip-board skill or vendor the generator."
        )
    spec = importlib.util.spec_from_file_location("flip_pdf_gen", _SKILL_PDF)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["flip_pdf_gen"] = mod
    spec.loader.exec_module(mod)
    return mod


_MOD = None


def _mod():
    global _MOD
    if _MOD is None:
        _MOD = _load()
    return _MOD


def build_deal_pdf(deal: dict, out_path: str):
    """Generate individual deal PDF."""
    m = _mod()
    metrics = m.compute_metrics(deal)
    m.build_deal_pdf(deal, metrics, out_path)
    return out_path


def build_comparison_pdf(deals: list, out_path: str):
    """Generate multi-deal comparison PDF.

    Builds a board-like dict the generator expects, computes metrics
    for each deal, then defers to the canonical comparison routine.
    """
    m = _mod()
    if len(deals) < 2:
        raise ValueError("Need at least 2 deals for comparison.")
    board = {"deals": deals}
    all_metrics = [m.compute_metrics(d) for d in deals]
    m.build_comparison_pdf(board, all_metrics, out_path)
    return out_path
