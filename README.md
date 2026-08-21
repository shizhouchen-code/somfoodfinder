# Food and Shows

This app helps you discover:
- SOM events with potential food mentions
- Yale performances ("Shows@Yale") in a selected date range

It includes:
- A CLI scraper (`food_calendar_scraper.py`)
- A Flask web app (`app.py`) with two tabs:
  - `SOM Food Finder`
  - `Shows@Yale`

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

### Web UI

```powershell
python .\app.py
```

Then open `http://127.0.0.1:5000` in your browser.

### Render / production

Use this start command (also in `Procfile`):

```bash
gunicorn --bind 0.0.0.0:$PORT --timeout 120 --workers 2 app:app
```

Find Food scans event pages in parallel and stops around 25 seconds so requests stay under Render's HTTP time limit.

## Environment variables

- `OPENAI_API_KEY`: required for AI-assisted food detection
- `OPENAI_MODEL`: defaults to `gpt-4.1-mini`
- `PASSWORD`: app login password (used when the password gate is on)
- `PASSWORD_GATE`: when `true` (default) or unset, the app shows the password screen; set to `false`, `0`, `no`, or `off` to disable the gate (e.g. public hosting)
- `FLASK_SECRET_KEY`: required for secure Flask sessions

### CLI

```powershell
python .\food_calendar_scraper.py --start-date 2026-04-01 --end-date 2026-04-30 --url "https://groups.som.yale.edu/events" --output food_events.json --headless
```

Use `--output food_events.csv` for CSV output.

## Notes

- The script tries common date-filter inputs/buttons on the page.
- If the site layout changes, selectors may need a small update in `apply_date_filter()` and `collect_event_links()`.
- For events without an explicit `Food Provided` marker, the app uses OpenAI to infer whether food is served from event details and stores the supporting quote in `Food Match`.
