import json
import html
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from flask import Flask, redirect, render_template, request, session, url_for
from dotenv import load_dotenv
from openai import OpenAI

from food_calendar_scraper import validate_date

app = Flask(__name__)
SOM_MOBILE_EVENTS_API = "https://groups.som.yale.edu/mobile_ws/v17/mobile_events_list"
YALE_SHOWS_API_URL = "https://events.yale.edu/api/2/events"
YALE_PERFORMANCES_EVENT_TYPE_ID = "46013551058602"
# Keep Find Food under Render's ~30s request limit.
FOOD_CHECK_WORKERS = 8
FOOD_CHECK_PAGE_TIMEOUT = 12
FOOD_CHECK_BUDGET_SECONDS = 25
FOOD_CHECK_OPENAI_MIN_REMAINING = 8
SOM_SEARCH_MAX_DAYS_AHEAD = 14
load_dotenv()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
APP_PASSWORD = os.getenv("PASSWORD", "")
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-me-in-env")


def _password_gate_enabled() -> bool:
    raw = os.getenv("PASSWORD_GATE")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


PASSWORD_GATE_ENABLED = _password_gate_enabled()


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


def som_search_window() -> tuple[str, str]:
    """Inclusive YYYY-MM-DD bounds for SOM Food Finder (today through two weeks ahead)."""
    today = datetime.now().date()
    latest = today + timedelta(days=SOM_SEARCH_MAX_DAYS_AHEAD)
    return today.strftime("%Y-%m-%d"), latest.strftime("%Y-%m-%d")


def validate_som_search_dates(start_date_str: str, end_date_str: str) -> None:
    start_date = validate_date(start_date_str)
    end_date = validate_date(end_date_str)
    if start_date > end_date:
        raise ValueError("Start date must be earlier than or equal to end date.")

    today = datetime.now().date()
    latest = today + timedelta(days=SOM_SEARCH_MAX_DAYS_AHEAD)
    if start_date < today or end_date > latest:
        raise ValueError(
            f"SOM Food Finder is limited to today through {SOM_SEARCH_MAX_DAYS_AHEAD} days ahead "
            f"({today.isoformat()} to {latest.isoformat()})."
        )


def build_shows_yale_url(start_date_str: str, end_date_str: str) -> str:
    start_date = validate_date(start_date_str)
    end_date = validate_date(end_date_str)
    if start_date > end_date:
        raise ValueError("start-date must be earlier than or equal to end-date.")

    # Yale Events API behaves like the end date is exclusive, so add 1 day
    # to preserve an inclusive date range in the UI.
    api_end_date = end_date + timedelta(days=1)
    params = {
        "start": start_date.strftime("%Y-%m-%d"),
        "end": api_end_date.strftime("%Y-%m-%d"),
        "pp": "100",
    }
    return f"{YALE_SHOWS_API_URL}?{urlencode(params, quote_via=quote)}"


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


def _match_food_heuristics(details_text: str, title: str = "") -> tuple[bool, str]:
    """Catch common food phrasing without calling OpenAI."""
    search_text = f"{title} {details_text}".strip()
    if not search_text:
        return False, ""

    provided_match = re.search(
        r"\b((?:lunch|dinner|breakfast|brunch|meal|food|snacks?|refreshments)"
        r"(?:\s+will\s+be|\s+is|\s+are)?\s+provided!?)",
        search_text,
        flags=re.IGNORECASE,
    )
    if provided_match:
        return True, f'"{provided_match.group(1)}"'

    meal_event_match = re.search(
        r"\b((?:welcome\s+)?(?:dinner|lunch|brunch|breakfast)|"
        r"(?:pizza|taco|bbq|barbecue)\s+night)\b",
        search_text,
        flags=re.IGNORECASE,
    )
    if meal_event_match:
        return True, f'"{meal_event_match.group(1)}"'

    legacy_keywords = [
        "lunch provided",
        "food provided",
        "dinner provided",
        "breakfast provided",
        "meal provided",
        "refreshments provided",
    ]
    details_lower = details_text.lower()
    matched_keyword = next((k for k in legacy_keywords if k in details_lower), "")
    if matched_keyword:
        return True, f"Event details mention '{matched_keyword}'"

    return False, ""


def _check_event_food_mentions(
    event_url: str,
    title: str = "",
    allow_openai: bool = True,
) -> tuple[bool, str]:
    request = Request(
        event_url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urlopen(request, timeout=FOOD_CHECK_PAGE_TIMEOUT) as response:
        page_html = response.read().decode("utf-8", errors="ignore")

    page_text = _clean_text(html.unescape(page_html))
    details_text = _extract_event_details_text(page_html)
    details_text_lower = details_text.lower()

    # Exclude "bring your own" phrasing to avoid false positives.
    exclusion_patterns = [
        r"\bbring (?:your own|your)\s+(?:lunch|dinner|breakfast|food|meal)\b",
        r"\bbyo\b",
        r"\bbrown bag\b",
        r"\bfood (?:not|isn't|is not)\s+provided\b",
        r"\bno (?:food|meal|lunch|dinner|breakfast|snacks?)\s+(?:provided|will be served)\b",
    ]
    if any(re.search(pattern, details_text_lower) for pattern in exclusion_patterns):
        return False, ""

    has_food_tag = bool(re.search(r"food\s*provided", page_text, flags=re.IGNORECASE))
    if has_food_tag:
        return True, "Food Provided tag"

    matched, reason = _match_food_heuristics(details_text, title=title)
    if matched:
        return True, reason

    if not allow_openai:
        return False, ""

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


def find_food_events(events: list[dict[str, str]]) -> tuple[list[dict[str, str]], bool]:
    """Return matching food events and whether the scan stopped early for time."""
    if not events:
        return [], False

    deadline = time.monotonic() + FOOD_CHECK_BUDGET_SECONDS
    food_events: list[dict[str, str]] = []
    timed_out = False

    def check_one(event: dict[str, str]) -> dict[str, str] | None:
        allow_openai = (deadline - time.monotonic()) >= FOOD_CHECK_OPENAI_MIN_REMAINING
        try:
            has_food, reason = _check_event_food_mentions(
                event["link"],
                title=event.get("title", ""),
                allow_openai=allow_openai,
            )
            if has_food:
                enriched = dict(event)
                enriched["food_match_reason"] = reason
                return enriched
        except Exception:
            return None
        return None

    executor = ThreadPoolExecutor(max_workers=FOOD_CHECK_WORKERS)
    try:
        futures = [executor.submit(check_one, event) for event in events]
        for future in as_completed(futures):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                result = future.result(timeout=remaining)
            except Exception:
                continue
            if result:
                food_events.append(result)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    return food_events, timed_out


def _format_iso_datetime_for_table(value: str) -> tuple[str, str]:
    if not value:
        return "", ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value, ""
    date_text = parsed.strftime("%a, %b %d, %Y").replace(" 0", " ")
    time_text = parsed.strftime("%I:%M %p").lstrip("0")
    return date_text, time_text


def _extract_price_from_offer(offer_data: object) -> str:
    if isinstance(offer_data, dict):
        return _clean_text(str(offer_data.get("price", "") or ""))
    if isinstance(offer_data, list):
        prices = []
        for offer in offer_data:
            if isinstance(offer, dict):
                price_text = _clean_text(str(offer.get("price", "") or ""))
                if price_text:
                    prices.append(price_text)
        return ", ".join(prices)
    return ""


def fetch_shows_events_from_yale(url: str) -> list[dict[str, str]]:
    def is_performance_event(event_data: dict) -> bool:
        filters = event_data.get("filters", {})
        if not isinstance(filters, dict):
            return False
        event_types = filters.get("event_types", [])
        if not isinstance(event_types, list):
            return False

        for event_type in event_types:
            if not isinstance(event_type, dict):
                continue
            type_id = str(event_type.get("id", "") or "")
            type_name = _clean_text(str(event_type.get("name", "") or ""))
            if type_id == YALE_PERFORMANCES_EVENT_TYPE_ID and type_name == "Performances":
                return True
        return False

    def fetch_payload(page_number: int | None = None) -> dict:
        request_url = url
        if page_number is not None:
            split = urlsplit(url)
            query_params = dict(parse_qsl(split.query, keep_blank_values=True))
            query_params["page"] = str(page_number)
            request_url = urlunsplit((split.scheme, split.netloc, split.path, urlencode(query_params, quote_via=quote), split.fragment))

        request_obj = Request(
            request_url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            },
        )
        with urlopen(request_obj, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    first_payload = fetch_payload()
    page_info = first_payload.get("page", {}) if isinstance(first_payload, dict) else {}
    page_total = 1
    if isinstance(page_info, dict):
        try:
            page_total = max(1, int(page_info.get("total", 1)))
        except (TypeError, ValueError):
            page_total = 1

    all_payloads: list[dict] = [first_payload]
    for page_number in range(2, page_total + 1):
        all_payloads.append(fetch_payload(page_number=page_number))

    events: list[dict[str, str]] = []
    for payload in all_payloads:
        for event_wrapper in payload.get("events", []):
            if not isinstance(event_wrapper, dict):
                continue
            event_data = event_wrapper.get("event", {})
            if not isinstance(event_data, dict):
                continue
            if not is_performance_event(event_data):
                continue

            instances = event_data.get("event_instances", [])
            instance_data = {}
            if isinstance(instances, list) and instances:
                first_instance = instances[0]
                if isinstance(first_instance, dict):
                    instance_data = first_instance.get("event_instance", {}) or {}
                    if not isinstance(instance_data, dict):
                        instance_data = {}

            start_iso = str(instance_data.get("start", "") or "")
            end_iso = str(instance_data.get("end", "") or "")
            date_text, start_time_text = _format_iso_datetime_for_table(start_iso)
            _, end_time_text = _format_iso_datetime_for_table(end_iso)

            all_day = bool(instance_data.get("all_day"))
            if all_day:
                time_text = "All day"
            elif start_time_text and end_time_text:
                time_text = f"{start_time_text} - {end_time_text}"
            else:
                time_text = start_time_text

            price_text = _clean_text(str(event_data.get("ticket_cost", "") or ""))
            if not price_text and event_data.get("free") is True:
                price_text = "Free"

            location_text = _clean_text(str(event_data.get("location_name", "") or ""))
            link_text = _clean_text(str(event_data.get("localist_url", "") or event_data.get("url", "") or ""))

            events.append(
                {
                    "title": _clean_text(str(event_data.get("title", "") or "")),
                    "date": date_text,
                    "time": time_text,
                    "location": location_text,
                    "price": price_text,
                    "link": link_text,
                }
            )

    return [event for event in events if event.get("title")]


@app.route("/", methods=["GET", "POST"])
def index():
    is_authenticated = (not PASSWORD_GATE_ENABLED) or bool(session.get("authenticated"))
    som_events = []
    shows_events = []
    som_error = ""
    shows_error = ""
    som_message = ""
    shows_message = ""
    auth_error = ""
    shows_form_data = {
        "start_date": datetime.now().strftime("%Y-%m-%d"),
        "end_date": datetime.now().strftime("%Y-%m-%d"),
    }
    som_api_url = ""
    shows_url = ""
    active_tab = "som"
    som_submitted = False
    shows_submitted = False
    som_min_date, som_max_date = som_search_window()
    som_form_data = {
        "start_date": som_min_date,
        "end_date": som_min_date,
    }

    if request.method == "POST":
        if request.form.get("action") == "unlock":
            submitted_password = request.form.get("password", "")
            if APP_PASSWORD and submitted_password == APP_PASSWORD:
                session["authenticated"] = True
                return redirect(url_for("index"))
            auth_error = "Incorrect password."
        elif not is_authenticated:
            auth_error = "Please enter the password to access the app."

    if is_authenticated and request.method == "POST":
        action = request.form.get("action", "fetch_events")
        active_tab = request.form.get("tab", "som")

        if active_tab == "shows":
            shows_submitted = True
            shows_form_data["start_date"] = request.form.get("shows_start_date", "").strip()
            shows_form_data["end_date"] = request.form.get("shows_end_date", "").strip()
            try:
                shows_url = build_shows_yale_url(shows_form_data["start_date"], shows_form_data["end_date"])
                if action != "fetch_shows":
                    raise ValueError("Unsupported action.")
                shows_events = fetch_shows_events_from_yale(shows_url)
                shows_message = f"Loaded {len(shows_events)} show event(s) from Yale Events."
            except Exception as exc:
                shows_error = str(exc)
        else:
            active_tab = "som"
            som_submitted = True
            som_form_data["start_date"] = request.form.get("som_start_date", "").strip()
            som_form_data["end_date"] = request.form.get("som_end_date", "").strip()
            try:
                validate_som_search_dates(som_form_data["start_date"], som_form_data["end_date"])
                som_api_url = build_mobile_events_api_url(som_form_data["start_date"], som_form_data["end_date"])
                if action == "fetch_events":
                    som_events = fetch_events_from_mobile_api(som_api_url)
                    som_message = f"Loaded {len(som_events)} event(s) from mobile JSON API."
                elif action == "find_food":
                    som_events = fetch_events_from_mobile_api(som_api_url)
                    som_events, food_scan_timed_out = find_food_events(som_events)
                    som_message = f"Found {len(som_events)} event(s) with food mentions."
                    if food_scan_timed_out:
                        som_error = (
                            "Too many events to scan in one request — showing partial results. "
                            "Narrow the date range and try again."
                        )
                else:
                    raise ValueError("Unsupported action.")
            except Exception as exc:
                som_error = str(exc)

    return render_template(
        "index.html",
        som_events=som_events,
        shows_events=shows_events,
        som_error=som_error,
        shows_error=shows_error,
        som_message=som_message,
        shows_message=shows_message,
        auth_error=auth_error,
        is_authenticated=is_authenticated,
        som_form_data=som_form_data,
        shows_form_data=shows_form_data,
        som_min_date=som_min_date,
        som_max_date=som_max_date,
        som_api_url=som_api_url,
        shows_url=shows_url,
        active_tab=active_tab,
        som_submitted=som_submitted,
        shows_submitted=shows_submitted,
    )


if __name__ == "__main__":
    app.run(debug=False, use_reloader=False, port=5000)
