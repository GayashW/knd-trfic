#!/usr/bin/env python3
"""
Virtual Floating Car – Kandy Traffic Monitor
Hardened for GitHub Actions (Ubuntu 24.04)
"""

import asyncio
import csv
import re
import time
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

# ---------------- CONFIG ----------------

OD_PAIRS = [
    {
        "name": "Peradeniya → KMTT",
        "origin": "Peradeniya, Sri Lanka",
        "destination": "Kandy Municipal Transport Terminal, Sri Lanka",
    },
    {
        "name": "Temple → Railway",
        "origin": "Sri Dalada Maligawa, Kandy, Sri Lanka",
        "destination": "Kandy Railway Station, Sri Lanka",
    },
]

CSV_FILE = "kandy_od_traffic.csv"
SCREENSHOT_DIR = Path("screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

CSV_HEADERS = [
    "timestamp_utc",
    "od_pair",
    "origin",
    "destination",
    "time_min",
    "distance_km",
    "avg_speed_kmh",
    "process_time_sec",
    "status",
]

MAX_RETRIES = 2

# ---------------- PARSERS ----------------

def parse_time_minutes(text: str):
    h = re.search(r"(\d+)\s*h", text)
    m = re.search(r"(\d+)\s*min", text)
    total = 0
    if h:
        total += int(h.group(1)) * 60
    if m:
        total += int(m.group(1))
    return total if total > 0 else None


def parse_distance_km(text: str):
    km = re.search(r"([\d.]+)\s*km", text)
    m = re.search(r"([\d.]+)\s*m\b", text)
    if km:
        return float(km.group(1))
    if m:
        return float(m.group(1)) / 1000
    return None

# ---------------- SCRAPER ----------------

async def handle_consent(page):
    for frame in page.frames:
        if "consent.google.com" in frame.url:
            btn = frame.locator("button:has-text('Accept all')")
            if await btn.count() > 0:
                await btn.first.click()
                await page.wait_for_timeout(1500)

async def scrape_route(page, od):
    start = time.time()

    result = {
        "timestamp_utc": datetime.utcnow().isoformat(),
        "od_pair": od["name"],
        "origin": od["origin"],
        "destination": od["destination"],
        "time_min": None,
        "distance_km": None,
        "avg_speed_kmh": None,
        "process_time_sec": 0,
        "status": "failed",
    }

    url = f"https://www.google.com/maps/dir/{od['origin']}/{od['destination']}/"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"[{od['name']}] Attempt {attempt}")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await handle_consent(page)

            await page.wait_for_selector("div[role='main']", timeout=60000)
            await page.wait_for_timeout(3000)

            # Force route activation
            btns = page.locator("div[role='button']:has-text('min')")
            if await btns.count() > 0:
                await btns.first.click()
                await page.wait_for_timeout(2000)

            content = await page.inner_text("div[role='main']")

            t_match = re.search(r"\d+\s*(?:h\s*)?\d*\s*min", content)
            d_match = re.search(r"[\d.]+\s*(?:km|m)\b", content)

            if not t_match or not d_match:
                raise ValueError("ETA or distance not found")

            result["time_min"] = parse_time_minutes(t_match.group(0))
            result["distance_km"] = parse_distance_km(d_match.group(0))

            if result["time_min"] and result["distance_km"]:
                result["avg_speed_kmh"] = round(
                    result["distance_km"] / (result["time_min"] / 60), 2
                )
                result["status"] = "success"
                break

        except Exception as e:
            print(f"  ❌ {e}")
            if attempt == MAX_RETRIES:
                screenshot = SCREENSHOT_DIR / f"{od['name'].replace(' ', '_')}.png"
                await page.screenshot(path=screenshot)
                result["status"] = f"error: {str(e)[:40]}"

    result["process_time_sec"] = round(time.time() - start, 2)
    print(f"  → {result['status']}")
    return result

# ---------------- MAIN ----------------

async def main():
    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="Asia/Colombo",
        )

        page = await context.new_page()

        for od in OD_PAIRS:
            results.append(await scrape_route(page, od))
            await asyncio.sleep(3)

        await browser.close()

    file_exists = Path(CSV_FILE).exists()
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        if not file_exists:
            writer.writeheader()
        for r in results:
            writer.writerow(r)

    print(f"\nSaved {len(results)} rows → {CSV_FILE}")

if __name__ == "__main__":
    asyncio.run(main())
