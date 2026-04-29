import argparse
import csv
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Iterable, List, Optional
from urllib.parse import urljoin

from dateutil import parser as date_parser
from playwright.sync_api import BrowserContext, Page, sync_playwright


DEFAULT_URL = "https://groups.som.yale.edu/events"


@dataclass
class EventRecord:
    title: str
    date: str
    time: str
    location: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape events within a date range and return key event details."
    )
    parser.add_argument("--start-date", required=True, help="Start date in YYYY-MM-DD format.")
    parser.add_argument("--end-date", required=True, help="End date in YYYY-MM-DD format.")
    parser.add_argument("--url", default=DEFAULT_URL, help="Events calendar URL.")
    parser.add_argument(
        "--output",
        default="events.json",
        help="Output file path (.json or .csv). Default: events.json",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode. Recommended for automation.",
    )
    return parser.parse_args()


def validate_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Invalid date '{value}'. Use YYYY-MM-DD.") from exc


def parse_event_datetime(raw_text: str) -> tuple[str, str]:
    cleaned = " ".join(raw_text.split())
    if not cleaned:
        return "", ""
    try:
        dt = date_parser.parse(cleaned, fuzzy=True)
        date_part = dt.strftime("%Y-%m-%d")
        time_part = dt.strftime("%I:%M %p").lstrip("0")
        return date_part, time_part
    except Exception:
        return cleaned, ""


def apply_date_filter(page: Page, start_date: date, end_date: date) -> None:
    start_iso = start_date.isoformat()
    end_iso = end_date.isoformat()

    # Try a broad set of date input selectors.
    start_selectors = [
        "input[name*=start i]",
        "input[id*=start i]",
        "input[placeholder*=start i]",
        "input[type='date']",
    ]
    end_selectors = [
        "input[name*=end i]",
        "input[id*=end i]",
        "input[placeholder*=end i]",
        "input[type='date']",
    ]

    def first_visible(selectors: Iterable[str]) -> Optional[str]:
        for selector in selectors:
            count = page.locator(selector).count()
            for idx in range(count):
                loc = page.locator(selector).nth(idx)
                if loc.is_visible():
                    return f"{selector} >> nth={idx}"
        return None

    start_selector = first_visible(start_selectors)
    end_selector = first_visible(end_selectors)

    if not start_selector or not end_selector:
        return

    page.fill(start_selector, start_iso)
    if start_selector == end_selector:
        if page.locator(end_selector).count() > 1:
            page.fill(f"{end_selector} >> nth=1", end_iso)
    else:
        page.fill(end_selector, end_iso)

    submit_selectors = [
        "button:has-text('Apply')",
        "button:has-text('Filter')",
        "button:has-text('Search')",
        "input[type='submit']",
    ]
    for selector in submit_selectors:
        loc = page.locator(selector)
        if loc.count() > 0 and loc.first.is_visible():
            loc.first.click()
            page.wait_for_load_state("networkidle")
            return

    page.keyboard.press("Enter")
    page.wait_for_timeout(1200)


def collect_event_links(page: Page, base_url: str) -> List[str]:
    links = page.locator("a[href*='event']")
    hrefs: List[str] = []
    for idx in range(links.count()):
        href = links.nth(idx).get_attribute("href")
        if not href:
            continue
        absolute = urljoin(base_url, href)
        if absolute not in hrefs:
            hrefs.append(absolute)
    return hrefs


def extract_events_from_listing(page: Page, base_url: str) -> List[EventRecord]:
    results: List[EventRecord] = []
    seen_keys: set[str] = set()

    anchors = page.locator("a[href*='/event/'], a[href*='event']")
    skip_title_prefixes = (
        "share ",
        "register",
        "i'm interested",
        "show all events",
        "save to ",
        "log in",
        "search",
    )
    for idx in range(anchors.count()):
        anchor = anchors.nth(idx)
        href = anchor.get_attribute("href")
        if not href:
            continue
        if "share" in href.lower():
            continue

        title = " ".join(anchor.inner_text().split()).strip()
        if not title or len(title) < 3:
            continue
        lowered = title.lower()
        if lowered in {"events", "event", "home", "groups"}:
            continue
        if lowered.startswith(skip_title_prefixes):
            continue

        absolute = urljoin(base_url, href)
        key = f"{title}|{absolute}"
        if key in seen_keys:
            continue

        context_text = ""
        try:
            context_text = anchor.locator("xpath=ancestor::*[self::li or self::article or self::div][1]").inner_text()
        except Exception:
            context_text = page.inner_text("body")

        parsed_date, parsed_time = parse_event_datetime(context_text[:350])
        location = extract_location(context_text)
        results.append(
            EventRecord(
                title=title,
                date=parsed_date,
                time=parsed_time,
                location=location,
            )
        )
        seen_keys.add(key)

    return results


def event_in_range(text: str, start_date: date, end_date: date) -> bool:
    try:
        parsed = date_parser.parse(text, fuzzy=True).date()
        return start_date <= parsed <= end_date
    except Exception:
        return False


def extract_location(page_text: str) -> str:
    normalized = re.sub(r"\s+", " ", page_text)
    patterns = [
        r"Location\s*:\s*([^.|\n]{2,120})",
        r"Where\s*:\s*([^.|\n]{2,120})",
        r"Venue\s*:\s*([^.|\n]{2,120})",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip(" -|")
    return ""


def scrape_events_in_context(
    context: BrowserContext, url: str, start_date: date, end_date: date
) -> List[EventRecord]:
    page = context.new_page()
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(1800)
    body_preview = page.inner_text("body")[:1500].lower()
    if "login is required" in body_preview or page.title().strip().lower() == "login":
        page.close()
        raise PermissionError(
            "This website requires login before events are visible. "
            "Please sign in first, then rerun."
        )

    apply_date_filter(page, start_date, end_date)

    listing_records = extract_events_from_listing(page, url)
    if listing_records:
        page.close()
        return listing_records

    links = collect_event_links(page, url)
    results: List[EventRecord] = []
    detail_page = context.new_page()

    for link in links:
        try:
            detail_page.goto(link, wait_until="domcontentloaded")
            detail_page.wait_for_timeout(600)
            body_text = detail_page.inner_text("body")

            title = detail_page.title().strip()
            # Prefer machine-readable time element where possible.
            date_text = ""
            if detail_page.locator("time").count() > 0:
                date_text = detail_page.locator("time").first.inner_text().strip()
            else:
                date_text = body_text[:300]
            parsed_date, parsed_time = parse_event_datetime(date_text)

            location = extract_location(body_text)

            results.append(
                EventRecord(
                    title=title,
                    date=parsed_date,
                    time=parsed_time,
                    location=location,
                )
            )
        except Exception:
            continue

    detail_page.close()
    page.close()
    return results


def write_output(events: List[EventRecord], output_path: str) -> None:
    if output_path.lower().endswith(".csv"):
        with open(output_path, "w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=["title", "date", "time", "location"])
            writer.writeheader()
            for event in events:
                writer.writerow(asdict(event))
        return

    with open(output_path, "w", encoding="utf-8") as file:
        json.dump([asdict(event) for event in events], file, indent=2)


def run_scrape(url: str, start_date_str: str, end_date_str: str, headless: bool = True) -> List[EventRecord]:
    start_date = validate_date(start_date_str)
    end_date = validate_date(end_date_str)
    if start_date > end_date:
        raise ValueError("start-date must be earlier than or equal to end-date.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        try:
            return scrape_events_in_context(context, url, start_date, end_date)
        finally:
            context.close()
            browser.close()


def main() -> None:
    args = parse_args()
    events = run_scrape(
        url=args.url,
        start_date_str=args.start_date,
        end_date_str=args.end_date,
        headless=args.headless,
    )

    write_output(events, args.output)

    print(f"Found {len(events)} event(s) in range.")
    print(f"Output written to: {args.output}")


if __name__ == "__main__":
    main()
