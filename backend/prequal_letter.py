"""Fix-and-flip financing pre-qualification letter generator.

Renders the borrower's pre-qualification letter (The Renté Group letterhead)
as a PDF — a faithful reproduction of the source Word document. Only the
date (today) and the subject-property address change per deal.

Exact formatting matched from the .docx:
  - Font: Arial (Helvetica is the identical PDF-standard equivalent)
  - "THE RENTÉ GROUP": 20pt bold, centered, #1B2A4A (navy)
  - Tagline: 7pt bold, centered, #B68A3C (gold)
  - Contact line: 8pt, centered, #555555 (gray)
  - Body: 11pt black, justified; 3rd paragraph bold
  - "Lior Avital": 11pt bold #1B2A4A; signature contacts 10pt #555555
Uses reportlab (a pip dependency) so it works on the server/Render too.
"""

from datetime import datetime

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_JUSTIFY, TA_CENTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.colors import HexColor, black
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

NAVY = HexColor("#1B2A4A")
GOLD = HexColor("#B68A3C")
GRAY = HexColor("#555555")

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

# NOTE: this paragraph is BOLD in the source document.
BODY_3 = ("This letter is not a commitment to lend, a loan approval, or a guarantee of "
          "financing. Any financing remains subject to, among other things: full "
          "underwriting; verification of the borrower’s credit, liquidity, and investment "
          "experience; satisfactory evaluation of the subject property, including as-is "
          "value, after-repair value (ARV), and renovation scope; title and insurance "
          "review; and final lender approval and issuance of formal loan documents. Final "
          "terms, rates, and loan amounts will be determined by the funding lender.")

BODY_4 = ("We are pleased to be working with Mr. Hazout and are available to discuss his "
          "qualifications further.")

# Letter-spaced tagline, exactly as in the document.
_TAGLINE = "C O M M E R C I A L   R E A L   E S T A T E   D E B T   &   C A P I T A L   A D V I S O R Y"


def _full_address(deal: dict) -> str:
    if deal.get("address"):
        a = str(deal["address"]).strip()
        tail = ", ".join(p for p in [deal.get("city", ""),
                                     f"{deal.get('state','')} {deal.get('zip','')}".strip()]
                          if p and p.strip())
        if tail and tail.lower() not in a.lower():
            a = f"{a}, {tail}"
        return a
    return "the subject property"


def build_prequal_pdf(deal: dict, out_path: str, when: datetime = None):
    when = when or datetime.now()
    try:
        date_str = when.strftime("%B %-d, %Y")
    except ValueError:
        date_str = when.strftime("%B %d, %Y").replace(" 0", " ")
    address = _full_address(deal)

    doc = SimpleDocTemplate(out_path, pagesize=LETTER,
                            leftMargin=1 * inch, rightMargin=1 * inch,
                            topMargin=1 * inch, bottomMargin=1 * inch,
                            title="Pre-Qualification Letter")

    FONT, BOLD = "Helvetica", "Helvetica-Bold"
    name = ParagraphStyle("name", fontName=BOLD, fontSize=20, textColor=NAVY,
                          alignment=TA_CENTER, leading=24, spaceAfter=3)
    tag = ParagraphStyle("tag", fontName=BOLD, fontSize=7, textColor=GOLD,
                         alignment=TA_CENTER, leading=10, spaceAfter=4)
    contact = ParagraphStyle("contact", fontName=FONT, fontSize=8, textColor=GRAY,
                             alignment=TA_CENTER, leading=11)
    p = ParagraphStyle("p", fontName=FONT, fontSize=11, textColor=black, leading=15, spaceAfter=11)
    pj = ParagraphStyle("pj", parent=p, alignment=TA_JUSTIFY)
    pjb = ParagraphStyle("pjb", parent=pj, fontName=BOLD)
    pb = ParagraphStyle("pb", parent=p, fontName=BOLD)
    sig_name = ParagraphStyle("sig_name", fontName=BOLD, fontSize=11, textColor=NAVY, leading=14)
    sig = ParagraphStyle("sig", fontName=FONT, fontSize=10, textColor=black, leading=13)
    sig_gray = ParagraphStyle("sig_gray", fontName=FONT, fontSize=10, textColor=GRAY, leading=13)

    def esc(s):
        return s.replace("&", "&amp;")

    e = []
    e.append(Paragraph("THE RENTÉ GROUP", name))
    e.append(Paragraph(esc(_TAGLINE).replace(" ", "&nbsp;"), tag))
    e.append(Paragraph("323 Sunny Isles Blvd, Suite 708, Sunny Isles Beach, FL 33160"
                       "&nbsp;&nbsp;&nbsp;|&nbsp;&nbsp;&nbsp;786-661-8316"
                       "&nbsp;&nbsp;&nbsp;|&nbsp;&nbsp;&nbsp;lior@therentegroup.com", contact))
    e.append(Spacer(1, 26))

    e.append(Paragraph(date_str, p))
    e.append(Spacer(1, 6))
    e.append(Paragraph("RE:&nbsp;&nbsp;Fix-and-Flip Financing Pre-Qualification — David Hazout", pb))
    e.append(Paragraph(f"Subject Property:&nbsp;&nbsp;{esc(address)}", pb))
    e.append(Spacer(1, 8))
    e.append(Paragraph("To Whom It May Concern:", p))

    e.append(Paragraph(BODY_1, pj))
    e.append(Paragraph(BODY_2, pj))
    e.append(Paragraph(BODY_3, pjb))   # bold paragraph
    e.append(Paragraph(BODY_4, pj))

    e.append(Spacer(1, 10))
    e.append(Paragraph("Sincerely,", p))
    e.append(Spacer(1, 18))
    e.append(Paragraph("Lior Avital", sig_name))
    e.append(Paragraph("Principal &amp; Head of Investments", sig))
    e.append(Paragraph("The Renté Group", sig))
    e.append(Paragraph("786-661-8316&nbsp;&nbsp;|&nbsp;&nbsp;lior@therentegroup.com", sig_gray))

    doc.build(e)
    return out_path
