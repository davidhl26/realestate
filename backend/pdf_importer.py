"""Extract property listings from PDFs using Claude.

User pastes a PDF (foreclosure auction list, MLS hot sheet, broker
inventory, courthouse docket, AI-generated comp report, etc.). We:

  1. Extract text from every page
  2. Send to Claude with a prompt that says "find every property and
     return a JSON array of {address, asking_price, beds, baths, sqft,
     year_built, ...}"
  3. Return the structured list so the user can review and bulk-create
     deals/leads/auctions
"""
import io
import json
import logging
import re
from typing import Optional

log = logging.getLogger("flip-board.pdf_importer")


def extract_text_from_pdf(pdf_bytes: bytes) -> dict:
    """Extract text + page count from a PDF.

    Tries pdfplumber first (preserves tables/structure), falls back to
    pypdf if pdfplumber chokes.
    """
    text_parts = []
    page_count = 0
    method = None

    # Strategy 1: pdfplumber (better for tables)
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            page_count = len(pdf.pages)
            for i, page in enumerate(pdf.pages, 1):
                t = page.extract_text() or ""
                if t.strip():
                    text_parts.append(f"--- PAGE {i} ---\n{t}")
                # Also try tables — useful for foreclosure auction PDFs
                try:
                    tables = page.extract_tables() or []
                    for tbl in tables:
                        for row in tbl:
                            line = " | ".join(str(c or "") for c in row).strip()
                            if line: text_parts.append(line)
                except Exception:
                    pass
        method = "pdfplumber"
    except Exception as e:
        log.warning("pdfplumber failed: %s — falling back to pypdf", e)

    # Strategy 2: pypdf
    if not text_parts:
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(pdf_bytes))
            page_count = len(reader.pages)
            for i, page in enumerate(reader.pages, 1):
                t = page.extract_text() or ""
                if t.strip():
                    text_parts.append(f"--- PAGE {i} ---\n{t}")
            method = "pypdf"
        except Exception as e:
            log.exception("pypdf also failed: %s", e)
            return {
                "ok": False,
                "error": f"Could not extract text: {e}",
                "page_count": 0,
                "text": "",
            }

    full_text = "\n\n".join(text_parts)
    return {
        "ok": True,
        "page_count": page_count,
        "method": method,
        "text": full_text,
        "char_count": len(full_text),
    }


# ============================================================================
# Claude extraction prompt
# ============================================================================

EXTRACT_SYSTEM = """You are a real-estate data extraction specialist. Given the
raw text of a PDF document, find EVERY property listing and return a structured
JSON array.

The PDF may be:
- A foreclosure auction list (case number, address, opening bid)
- An MLS hot sheet / broker inventory list
- A tax-deed auction PDF
- A courthouse docket
- An AI-generated comp report
- An investor distressed-property list
- A multi-property analysis report

For each property you find, extract everything available:

{
  "address": "<full street address with city/state/zip if available>",
  "city": "<if separable>",
  "state": "<2-letter>",
  "zip": "<5-digit>",
  "property_type": "Single Family / Condo / Townhouse / Multi-family / Land / Commercial",
  "beds": <int or null>,
  "baths": <float or null>,
  "sqft": <int or null>,
  "year_built": <int or null>,
  "lot_size": "<string like '7,400 sq ft' or '0.25 acres'>",
  "purchase_price": <int — asking price, opening bid, list price, whatever is listed>,
  "price_type": "asking | opening_bid | sold | judgment | estimated_value",
  "arv_base": <int — if PDF gives an ARV/after-repair value>,
  "rehab_base": <int — if PDF gives rehab estimate>,
  "case_number": "<if foreclosure>",
  "parcel_id": "<if listed>",
  "auction_date": "<YYYY-MM-DD if mentioned>",
  "image_url": "<if PDF embeds image links — rare>",
  "source_url": "<if PDF mentions a listing URL>",
  "notes": "<1-2 sentences: condition, motivation, key details. Be CONCISE.>"
}

CRITICAL RULES:
1. ONLY return data ACTUALLY PRESENT in the PDF. Do NOT fabricate.
2. If a field is unknown, OMIT it (don't write null/empty/unknown).
3. Address is REQUIRED — skip rows that don't have one.
4. Deduplicate — if the same address appears multiple times, return it once.
5. Return ALL properties found, not just the first few.
6. Convert "Opening Bid: $123,456" → purchase_price: 123456, price_type: "opening_bid"
7. For foreclosure PDFs, treat "Final Judgment Amount" as purchase_price with type "judgment"

Output ONE JSON code block with this exact schema, NO other text after:
{
  "doc_type": "<foreclosure_auction | mls_listing | tax_deed | broker_list | comp_report | other>",
  "doc_summary": "<1-sentence description of what this PDF is>",
  "properties": [ ... array of property objects ... ],
  "warnings": ["<any caveats — pages couldn't be read, suspicious data, etc>"]
}"""


def extract_properties_from_text(text: str, filename: str = "") -> dict:
    """Send extracted text to Claude → structured property list."""
    from . import ai_research
    api_key = ai_research.get_api_key()
    if not api_key:
        return {"ok": False, "error": "No Anthropic API key configured."}

    # Cap text — Claude can handle a lot but keep cost sane
    MAX_CHARS = 80000  # ~20-30 pages typical
    truncated = False
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n\n[…TRUNCATED…]"
        truncated = True

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    model = ai_research.get_model()

    user_msg = (f"Source filename: {filename}\n\n"
                 if filename else "") + f"PDF text:\n\n{text}"

    try:
        msg = client.messages.create(
            model=model,
            max_tokens=8000,
            system=EXTRACT_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
    except anthropic.AuthenticationError:
        return {"ok": False, "error": "Invalid API key", "error_type": "auth"}
    except anthropic.APIStatusError as e:
        if e.status_code == 402:
            return {"ok": False, "error": "Out of credits", "error_type": "billing"}
        return {"ok": False, "error": str(e)}
    except Exception as e:
        log.exception("Claude extraction failed")
        return {"ok": False, "error": str(e)}

    text_resp = "".join(b.text for b in msg.content if hasattr(b, "text"))

    # Pull the first JSON object
    data = None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text_resp, re.DOTALL)
    if m:
        try: data = json.loads(m.group(1))
        except json.JSONDecodeError: pass
    if not data:
        # Find balanced {} block
        depth, start = 0, None
        for i, ch in enumerate(text_resp):
            if ch == "{":
                if depth == 0: start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        data = json.loads(text_resp[start:i+1])
                        break
                    except json.JSONDecodeError:
                        start = None
    if not data:
        return {
            "ok": False,
            "error": "Could not parse Claude response as JSON",
            "raw_response": text_resp[:2000],
        }

    properties = data.get("properties", []) or []

    return {
        "ok": True,
        "doc_type": data.get("doc_type", "other"),
        "doc_summary": data.get("doc_summary", ""),
        "properties": properties,
        "warnings": data.get("warnings", []),
        "truncated": truncated,
        "model": model,
        "usage": {
            "input_tokens": msg.usage.input_tokens,
            "output_tokens": msg.usage.output_tokens,
        },
    }


# ============================================================================
# End-to-end orchestrator
# ============================================================================

def analyze_pdf(pdf_bytes: bytes, filename: str = "") -> dict:
    """Top-level: PDF bytes → structured property list."""
    extracted = extract_text_from_pdf(pdf_bytes)
    if not extracted.get("ok"):
        return extracted
    if not extracted.get("text"):
        return {
            "ok": False,
            "error": "No text found in PDF (may be a scanned image — needs OCR).",
            "page_count": extracted.get("page_count", 0),
        }

    log.info("Extracted %d chars from %d pages (%s) — sending to Claude",
              extracted["char_count"], extracted["page_count"],
              extracted.get("method"))

    parsed = extract_properties_from_text(extracted["text"], filename=filename)
    if not parsed.get("ok"):
        # Return raw text so user can see what was extracted
        parsed["raw_text_excerpt"] = extracted["text"][:3000]
        return parsed

    return {
        "ok": True,
        "filename": filename,
        "page_count": extracted["page_count"],
        "char_count": extracted["char_count"],
        "extraction_method": extracted.get("method"),
        "doc_type": parsed.get("doc_type"),
        "doc_summary": parsed.get("doc_summary"),
        "properties": parsed.get("properties", []),
        "warnings": parsed.get("warnings", []),
        "truncated": parsed.get("truncated", False),
        "model": parsed.get("model"),
        "usage": parsed.get("usage"),
    }
