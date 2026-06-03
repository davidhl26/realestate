# Flip Board — Local Desktop App

Fix-and-flip multi-deal evaluation platform with auto-fetch from Zillow/Redfin,
dashboard graphs, and PDF report generation. Runs entirely locally.

> **DISCLAIMER:** Educational/research purposes only. Not financial advice.

---

## Quick Start (macOS)

### First time setup
```bash
cd /Users/davidhazout/Desktop/ici/flip-board-app
chmod +x start.sh FlipBoard.command
./start.sh
```

The first run creates a Python virtualenv, installs dependencies (~1 min), and launches the app in a native window.

### Subsequent launches
- **Option A:** Double-click `FlipBoard.command` (Finder)
- **Option B:** Run `./start.sh` from the terminal
- **Option C:** Drag `FlipBoard.command` to your Dock

To suppress the brief terminal flash on launch, rename `FlipBoard.command` to `FlipBoard.tool` after first run, then use Automator (see "Polished icon" section below).

---

## Features

### Dashboard
- Aggregate stats: total deals, total profit, average ROI, average cap rate, 70%-rule pass rate
- Net Profit by Deal (bar chart)
- ROI vs Cap Rate (scatter plot)
- Score Distribution (bar chart)
- Top Deals list

### Deals
- Sortable table of all deals on the board
- Click any row to open the full analysis

### Deal Detail
- Circular score gauge (0-100) with grade
- **Flip-to-rent alert** when cap rate ≥ 9% and flip ROI ≤ 12%
- Quick numbers (purchase, ARV, rehab, all-in, profit, ROI, cap rate, BRRRR CF)
- Strategy comparison: Flip vs Rent vs BRRRR
- Scenario analysis (best/base/worst)
- **Max purchase back-solver** for 4 profit-margin targets
- 70% rule status
- Financing options comparison
- Risk factors with severity badges
- One-click PDF generation (rendered inline)

### Add Deal
- **Auto-fetch** by pasting a Zillow or Redfin URL
- Manual form with all fields (financials, rental, market, neighborhood)
- Edit existing deals

### Compare
- Multi-deal comparison PDF (landscape, 4 pages) with ranking, side-by-side metrics, best/worst call-outs, and strategy matrix

---

## Architecture

```
flip-board-app/
├── app.py                       # PyWebView launcher (entry point)
├── start.sh                     # Setup + launch script
├── FlipBoard.command            # macOS double-click launcher
├── requirements.txt             # Python dependencies
├── backend/
│   ├── server.py                # FastAPI server
│   ├── analyzer.py              # Metrics + scoring engine
│   ├── scraper.py               # Zillow/Redfin scraping
│   ├── pdf_gen.py               # PDF wrapper (uses flip-board skill)
│   └── db.py                    # JSON persistence
├── frontend/
│   ├── index.html               # Single-page app
│   ├── css/style.css
│   └── js/
│       ├── api.js               # API client
│       ├── app.js               # UI logic
│       └── chart.umd.min.js     # Chart.js (bundled offline)
└── data/
    ├── flip-board.json          # Deals database (auto-created)
    └── pdfs/                    # Generated PDFs cache
```

The PDF generator lives at `~/.claude/skills/flip-board/scripts/generate_flip_board_pdf.py` and is reused by both the skill (`/flip-board`) and this desktop app — both produce identical reports.

---

## How auto-fetch works

The scraper performs a single HTTP GET with realistic browser headers, then parses the embedded data in the listing page:
- **Zillow:** JSON-LD blocks + `__NEXT_DATA__` script + regex fallbacks
- **Redfin:** JSON-LD + inline JSON regex

No browser automation is used (no Playwright/Puppeteer), which keeps the app fast and lightweight.

**Limitations:**
- Zillow occasionally serves a "Press & Hold" captcha — when detected, the app flags `requires_manual_entry: true` and you fill the form by hand
- Listing pages change structure occasionally; selectors include multiple fallbacks
- Comp data (`arv_base`, `rehab_base`) is **not** scraped — these require human research or AI input

---

## Polished icon (optional)

To turn `FlipBoard.command` into a proper `.app` bundle that opens silently:

1. Open **Automator** → New → Application
2. Add the **Run Shell Script** action
3. Paste:
   ```bash
   /Users/davidhazout/Desktop/ici/flip-board-app/start.sh
   ```
4. Save as `FlipBoard.app` (Applications folder)
5. To customize the icon: right-click the `.app` → Get Info → drag a `.icns` file onto the icon preview

For a real packaged `.app` (no terminal, single bundle, custom icon), use **PyInstaller**:
```bash
source .venv/bin/activate
pip install pyinstaller
pyinstaller --windowed --onefile --name "FlipBoard" \
            --add-data "frontend:frontend" \
            --add-data "backend:backend" \
            app.py
```
The output is `dist/FlipBoard.app` — fully self-contained.

---

## Data location

All data lives in `data/flip-board.json`. To back up your board, copy that single file. To wipe the board, delete it (the app re-creates it empty on launch).

---

## Stopping the app

Close the window — the background server exits automatically.

---

## Troubleshooting

**Port already in use:**
The app picks a random free port if 8765 is busy. No action needed.

**Dependencies fail to install:**
```bash
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

**PyWebView complains about missing components on macOS:**
PyWebView 5.x uses WKWebView (built into macOS), no extra install required.

**Captcha on Zillow:**
Open the listing in a regular browser, complete the captcha, then retry the URL. Alternatively, fill the deal form manually.
