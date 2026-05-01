# Food Calendar Scraper

This app scrapes an events calendar page, opens each event link, and reports events that mention food (for example: "lunch provided" or "food provided").

It includes:
- A CLI scraper (`food_calendar_scraper.py`)
- A Pusheen-themed web UI (`app.py`)

## What it outputs

For each matching event:
- Title
- Date
- Time
- Link
- Matched food keyword

Output can be `.json` or `.csv`.

## Setup

1. Create and activate a virtual environment (recommended):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Install dependencies:

```powershell
pip install -r requirements.txt
playwright install chromium
```

3. Configure environment variables:

```powershell
copy .env.example .env
```

Then edit `.env` and set your OpenAI key:

```env
OPENAI_API_KEY=your_openai_api_key_here
OPENAI_MODEL=gpt-4.1-mini
```

## Run

### Web UI (Pusheen themed)

```powershell
python .\app.py
```

Then open `http://127.0.0.1:5000` in your browser.

### CLI

```powershell
python .\food_calendar_scraper.py --start-date 2026-04-01 --end-date 2026-04-30 --url "https://groups.som.yale.edu/events" --output food_events.json --headless
```

Use `--output food_events.csv` for CSV output.

## Notes

- The script tries common date-filter inputs/buttons on the page.
- If the site layout changes, selectors may need a small update in `apply_date_filter()` and `collect_event_links()`.
- For events without an explicit `Food Provided` marker, the app uses OpenAI to infer whether food is served from event details and stores the supporting quote in `Food Match`.
