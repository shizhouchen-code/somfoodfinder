import json
import html
import os
import re
from datetime import datetime
from urllib.parse import quote, urlencode, urljoin
from urllib.request import Request, urlopen

from flask import Flask, render_template, request
from dotenv import load_dotenv
from openai import OpenAI

from food_calendar_scraper import validate_date

app = Flask(__name__)
SOM_MOBILE_EVENTS_API = "https://groups.som.yale.edu/mobile_ws/v17/mobile_events_list"
load_dotenv()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")


def build_mobile_events_api_url(start_date_str: str, end_date_str: str) -> str:
    start_date = validate_date(start_date_str)
    end_date = validate_date(end_date_str)
    if start_date > end_date:
        raise ValueError("start-date must be earlier than or equal to end-date.")

    params = {
        "range": "0",
        "limit": "",
        "filter8": start_date.strftime("%d %b %Y"),
        "filter9": end_date.strftime("%d %b %Y"),
    }
    return f"{SOM_MOBILE_EVENTS_API}?{urlencode(params, quote_via=quote)}"


def _clean_text(value: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", value or "")
    return " ".join(no_tags.split())


def _parse_date_and_time(event_dates_raw: str) -> tuple[str, str]:
    unescaped = event_dates_raw or ""
    # API sometimes returns HTML escaped multiple times (e.g. &amp;lt;p&amp;gt;...).
    for _ in range(3):
        next_value = html.unescape(unescaped)
        if next_value == unescaped:
            break
        unescaped = next_value
    normalized = re.sub(r"</p\s*>", "\n", unescaped, flags=re.IGNORECASE)
    normalized = re.sub(r"<br\s*/?>", "\n", normalized, flags=re.IGNORECASE)
    no_tags = re.sub(r"<[^>]+>", " ", normalized)
    lines = [" ".join(line.split()) for line in no_tags.splitlines() if line.strip()]

    if len(lines) >= 2:
        return lines[0], lines[1]
    if len(lines) == 1:
        single_line = lines[0]
        time_match = re.search(
            r"\d{1,2}:\d{2}\s*[AP]M(?:\s*[–-]\s*\d{1,2}:\d{2}\s*[AP]M)?",
            single_line,
            flags=re.IGNORECASE,
        )
        if time_match:
            time_text = " ".join(time_match.group(0).split())
            date_text = single_line.replace(time_match.group(0), "").strip(" ,|-")
            return date_text, time_text
        return single_line, ""
    return "", ""


def fetch_events_from_mobile_api(api_url: str) -> list[dict[str, str]]:
    request = Request(
        api_url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))

    events: list[dict[str, str]] = []
    for item in payload:
        if item.get("listingSeparator") == "true":
            continue

        fields = [field.strip() for field in item.get("fields", "").split(",") if field.strip()]
        field_map: dict[str, str] = {}
        for idx, field_name in enumerate(fields):
            field_map[field_name] = str(item.get(f"p{idx}", "") or "")

        event_name = _clean_text(field_map.get("eventName", ""))
        if not event_name:
            continue

        event_date, event_time = _parse_date_and_time(field_map.get("eventDates", ""))
        event_date = _clean_text(html.unescape(event_date))
        event_time = _clean_text(html.unescape(event_time))
        time_range_match = re.search(
            r"(\d{1,2}:\d{2}\s*[AP]M).*?(\d{1,2}:\d{2}\s*[AP]M)",
            event_time,
            flags=re.IGNORECASE,
        )
        if time_range_match:
            event_time = f"{time_range_match.group(1)} - {time_range_match.group(2)}"
        event_location = _clean_text(field_map.get("eventLocation", ""))
        event_url = field_map.get("eventUrl", "")
        full_event_url = urljoin("https://groups.som.yale.edu", event_url)

        events.append(
            {
                "title": event_name,
                "date": event_date,
                "time": event_time,
                "category": _clean_text(field_map.get("eventCategory", "")),
                "location": event_location,
                "club": _clean_text(field_map.get("clubName", "")),
                "attendees": _clean_text(field_map.get("eventAttendees", "")),
                "price": _clean_text(field_map.get("eventPriceRange", "")),
                "registration_status": _clean_text(field_map.get("registrationStatus", "")),
                "link": full_event_url,
            }
        )

    return events


def _extract_event_details_text(page_html: str) -> str:
    details_match = re.search(
        r"<!--\s*Event Details\s*-->(.*?)(?:<!--\s*Event Dress Code\s*-->|<!--\s*Food\s*Provided\s*-->|<!--\s*More Details\s*-->|</div>\s*</div>)",
        page_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    details_block = details_match.group(1) if details_match else page_html
    return _clean_text(html.unescape(details_block))


def _check_event_food_mentions(event_url: str) -> tuple[bool, str]:
    request = Request(
        event_url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urlopen(request, timeout=30) as response:
        page_html = response.read().decode("utf-8", errors="ignore")

    page_text = _clean_text(html.unescape(page_html))
    details_text = _extract_event_details_text(page_html)

    has_food_tag = bool(re.search(r"food\s*provided", page_text, flags=re.IGNORECASE))
    details_keywords = [
        "lunch provided",
        "food provided",
        "dinner provided",
        "breakfast provided",
        "meal provided",
        "refreshments provided",
    ]
    details_text_lower = details_text.lower()
    # Exclude "bring your own" phrasing to avoid false positives.
    exclusion_patterns = [
        r"\bbring (?:your own|your)\s+(?:lunch|dinner|breakfast|food|meal)\b",
        r"\bbyo\b",
        r"\bbrown bag\b",
        r"\bfood (?:not|isn't|is not)\s+provided\b",
        r"\bno (?:food|meal|lunch|dinner|breakfast)\s+(?:provided|will be served)\b",
    ]
    if any(re.search(pattern, details_text_lower) for pattern in exclusion_patterns):
        return False, ""

    matched_keyword = next((k for k in details_keywords if k in details_text_lower), "")

    if has_food_tag:
        return True, "Food Provided tag"
    if matched_keyword:
        return True, f"Event details mention '{matched_keyword}'"

    try:
        return _check_food_with_openai(details_text)
    except Exception:
        return False, ""


def _check_food_with_openai(details_text: str) -> tuple[bool, str]:
    if not OPENAI_API_KEY:
        return False, ""
    if not details_text.strip():
        return False, ""

    client = OpenAI(api_key=OPENAI_API_KEY)
    # Ask for strict JSON so parsing stays deterministic.
    response = client.responses.create(
        model=OPENAI_MODEL,
        input=[
            {
                "role": "system",
                "content": (
                    "You determine if event details imply food is served. "
                    "Return strict JSON only with fields: serves_food (boolean), quote (string). "
                    "quote must be an exact phrase from the details if serves_food=true, else empty string. "
                    "If details suggest attendees should bring their own food (e.g., 'bring your lunch', BYO, brown bag), "
                    "then serves_food must be false."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Event details:\n"
                    f"{details_text}\n\n"
                    "Answer only as JSON object."
                ),
            },
        ],
        temperature=0,
    )
    raw = response.output_text.strip()
    if not raw:
        return False, ""

    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.replace("json", "", 1).strip()
    parsed = json.loads(raw)
    serves_food = bool(parsed.get("serves_food", False))
    quote = str(parsed.get("quote", "")).strip()
    if serves_food and quote:
        return True, f"\"{quote}\""
    return False, ""


def find_food_events(events: list[dict[str, str]]) -> list[dict[str, str]]:
    food_events: list[dict[str, str]] = []
    for event in events:
        try:
            has_food, reason = _check_event_food_mentions(event["link"])
            if has_food:
                enriched = dict(event)
                enriched["food_match_reason"] = reason
                food_events.append(enriched)
        except Exception:
            continue
    return food_events


@app.route("/", methods=["GET", "POST"])
def index():
    events = []
    error = ""
    message = ""
    form_data = {
        "start_date": datetime.now().strftime("%Y-%m-%d"),
        "end_date": datetime.now().strftime("%Y-%m-%d"),
    }
    api_url = ""

    if request.method == "POST":
        action = request.form.get("action", "fetch_events")
        form_data["start_date"] = request.form.get("start_date", "").strip()
        form_data["end_date"] = request.form.get("end_date", "").strip()

        try:
            api_url = build_mobile_events_api_url(form_data["start_date"], form_data["end_date"])
            if action == "fetch_events":
                events = fetch_events_from_mobile_api(api_url)
                message = f"Loaded {len(events)} event(s) from mobile JSON API."
            elif action == "find_food":
                events = fetch_events_from_mobile_api(api_url)
                events = find_food_events(events)
                message = f"Found {len(events)} event(s) with food mentions."
            else:
                raise ValueError("Unsupported action.")
        except Exception as exc:
            error = str(exc)

    return render_template(
        "index.html",
        events=events,
        error=error,
        message=message,
        form_data=form_data,
        api_url=api_url,
    )


if __name__ == "__main__":
    app.run(debug=False, use_reloader=False, port=5000)
