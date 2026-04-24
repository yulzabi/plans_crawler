# Ness Ziona Building Plans Crawler

Crawls the [Ness Ziona municipal building archive](https://nes.bartech-net.co.il/SearchBuildingArchive) to extract building permit data (owner, architect) from scanned PDFs.

## Setup

```bash
# Install system dependencies (macOS)
brew install tesseract tesseract-lang

# Install Python dependencies
pip install -r requirements.txt
python -m playwright install chromium
```

## Usage

```bash
# Start or resume crawling all building files
python -m src.main

# Check progress
python -m src.main --status

# Retry previously failed files
python -m src.main --retry-errors

# Manually fix records with missing owner/architect
python -m src.main --fix

# Fetch new entries from the portal (incremental update)
python -m src.main --update
```

## How it works

1. Searches the Bartech portal for all building files in Ness Ziona (~4100 files)
2. For each file, navigates to the detail page and finds היתר בניה (building permit) PDFs dated ≥2000
3. Downloads PDFs to `data/pdfs/{address}/`
4. OCRs with Tesseract (Hebrew) and extracts: owner (בעל היתר), architect (עורך הבקשה), address, gush, helka
5. Writes results to `data/output.csv`
6. State saved after each file — interrupt anytime with Ctrl+C, restart to resume

## Output

- `data/output.csv` — extracted permit data
- `data/pdfs/` — downloaded PDFs organized by address
- `data/state.json` — crawl progress (auto-managed)

## Known Limitations

- OCR accuracy on scanned permits varies (70-90%). Owner/architect fields are hardest due to table cell borders. Use `--fix` to manually correct.
- The portal's reCAPTCHA currently auto-solves (invisible v3). If it starts requiring manual solving, the crawler will pause and prompt you.

## Dashboard (planned)

A web dashboard for searching and browsing the extracted data. Tech options:
- **Simple:** GitHub Pages + static site with client-side search over the CSV
- **Interactive:** Streamlit or Datasette app reading from the CSV/SQLite
- **Full:** Next.js/React app with a proper backend

The CSV is designed to be dashboard-ready with all fields needed for filtering and search.
