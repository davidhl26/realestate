"""Fix-and-flip financing pre-qualification letter generator.

Renders the borrower's pre-qualification letter (The Renté Group letterhead)
as a PDF, dated today, with the deal's address as the subject property.
Uses reportlab (a pip dependency) so it works on the server/Render too.
"""

from datetime import datetime

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_JUSTIFY, TA_CENTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.colors import HexColor
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                HRFlowable)

NAVY = HexColor("#0d3b5e")
GRAY = HexColor("#5b6b78")

BODY_1 = ("The Renté Group has reviewed preliminary information provided by Mr. David "
          "Hazout in connection with short-term, business-purpose financing for the "
          "acquisition and renovation (“fix-and-flip”) of the property referenced "
          "above. Based on that preliminary review, Mr. Hazout appears to be "
          "pre-qualified for fix-and-flip / bridge financing through our lending network.")

BODY_2 = ("This pre-qualification is based solely on information furnished by the borrower "
          "and has not been independently verified. It reflects our preliminary assessment "
          "of the borrower’s general eligibility for business-purpose investment financing "
          "and indicates that Mr. Hazout is actively working with our firm to arrange "
          "capital for the project.")

BODY_3 = ("This letter is not a commitment to lend, a loan approval, or a guarantee of "
          "financing. Any financing remains subject to, among other things: full "
          "underwriting; verification of the borrower’s credit, liquidity, and investment "
          "experience; satisfactory evaluation of the subject property, including as-is "
          "value, after-repair value (ARV), and renovation scope; title and insurance "
          "review; and final lender approval and issuance of formal loan documents. Final "
          "terms, rates, and loan amounts will be determined by the funding lender.")

BODY_4 = ("We are pleased to be working with Mr. Hazout and are available to discuss his "
          "qualifications further.")


def _full_address(deal: dict) -> str:
    if deal.get("address"):
        a = str(deal["address"]).strip()
        # Append city/state/zip if the address line doesn't already include them
        tail = ", ".join(p for p in [deal.get("city", ""),
                                     f"{deal.get('state','')} {deal.get('zip','')}".strip()]
                          if p and p.strip())
        if tail and tail.lower() not in a.lower():
            a = f"{a}, {tail}"
        return a
    return "the subject property"


def build_prequal_pdf(deal: dict, out_path: str, when: datetime = None):
    when = when or datetime.now()
    date_str = when.strftime("%B %-d, %Y") if hasattr(when, "strftime") else str(when)
    address = _full_address(deal)

    doc = SimpleDocTemplate(out_path, pagesize=LETTER,
                            leftMargin=1 * inch, rightMargin=1 * inch,
                            topMargin=0.9 * inch, bottomMargin=0.9 * inch,
                            title="Pre-Qualification Letter")

    name = ParagraphStyle("name", fontName="Helvetica-Bold", fontSize=19,
                          textColor=NAVY, alignment=TA_CENTER, spaceAfter=2, leading=22)
    tag = ParagraphStyle("tag", fontName="Helvetica", fontSize=8, textColor=GRAY,
                         alignment=TA_CENTER, spaceAfter=4, leading=11)
    contact = ParagraphStyle("contact", fontName="Helvetica", fontSize=8.5, textColor=GRAY,
                             alignment=TA_CENTER, spaceAfter=2, leading=11)
    body = ParagraphStyle("body", fontName="Helvetica", fontSize=10.5, leading=15.5,
                          alignment=TA_JUSTIFY, spaceAfter=11, textColor=HexColor("#1d2b36"))
    plain = ParagraphStyle("plain", fontName="Helvetica", fontSize=10.5, leading=15.5,
                           spaceAfter=4, textColor=HexColor("#1d2b36"))
    bold = ParagraphStyle("bold", parent=plain, fontName="Helvetica-Bold")

    sp = " &nbsp;&nbsp; "
    tagline = ("C O M M E R C I A L" + sp + "R E A L" + sp + "E S T A T E" + sp +
               "D E B T" + sp + "&" + sp + "C A P I T A L" + sp + "A D V I S O R Y")

    e = []
    e.append(Paragraph("THE RENTÉ GROUP", name))
    e.append(Paragraph(tagline, tag))
    e.append(Paragraph("323 Sunny Isles Blvd, Suite 708, Sunny Isles Beach, FL 33160"
                       " &nbsp;|&nbsp; 786-661-8316 &nbsp;|&nbsp; lior@therentegroup.com", contact))
    e.append(Spacer(1, 8))
    e.append(HRFlowable(width="100%", thickness=1.1, color=NAVY))
    e.append(Spacer(1, 18))

    e.append(Paragraph(date_str, plain))
    e.append(Spacer(1, 10))
    e.append(Paragraph("RE: &nbsp; Fix-and-Flip Financing Pre-Qualification — David Hazout", bold))
    e.append(Paragraph(f"Subject Property: &nbsp; {address}", bold))
    e.append(Spacer(1, 12))
    e.append(Paragraph("To Whom It May Concern:", plain))
    e.append(Spacer(1, 8))

    for b in (BODY_1, BODY_2, BODY_3, BODY_4):
        e.append(Paragraph(b, body))

    e.append(Spacer(1, 16))
    e.append(Paragraph("Sincerely,", plain))
    e.append(Spacer(1, 20))
    e.append(Paragraph("Lior Avital", bold))
    e.append(Paragraph("Principal &amp; Head of Investments", plain))
    e.append(Paragraph("The Renté Group", plain))
    e.append(Paragraph("786-661-8316 &nbsp;|&nbsp; lior@therentegroup.com", plain))

    doc.build(e)
    return out_path
