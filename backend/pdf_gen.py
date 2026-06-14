"""Thin wrapper around the flip-board PDF generator.

Uses the VENDORED generator (backend/flip_pdf_gen.py) so PDF generation works
everywhere, including the Render server. Falls back to the user's skill-folder
copy only if the vendored module is somehow unavailable (local dev).
"""

import importlib.util
import sys
from pathlib import Path

# Skill-folder copy (local dev only) — used as a last-resort fallback.
_SKILL_PDF = Path.home() / ".claude" / "skills" / "flip-board" / "scripts" / "generate_flip_board_pdf.py"


def _load():
    # 1) Vendored copy shipped with the app (works on prod/Render).
    try:
        from . import flip_pdf_gen as mod
        return mod
    except Exception:
        pass
    # 2) Fallback: load from the skill folder if present (local dev).
    if _SKILL_PDF.exists():
        spec = importlib.util.spec_from_file_location("flip_pdf_gen_skill", _SKILL_PDF)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["flip_pdf_gen_skill"] = mod
        spec.loader.exec_module(mod)
        return mod
    raise RuntimeError("PDF generator unavailable (vendored module missing).")


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
