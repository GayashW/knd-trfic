#!/usr/bin/env python3
"""
Virtual Floating Car Traffic Monitor for Kandy, Sri Lanka
GitHub-Actions-safe Google Maps Directions scraper
"""

import asyncio
import csv
import os
import re
import time
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ---------------- CONFIG ----------------

OD_PAIRS = [
    {
        "name": "Peradeniya to KMTT",
        "origin": "Peradeniya, Sri Lanka",
        "destination": "Kandy Municipal Transport Terminal, Kandy, Sri Lanka",
    },
    {
        "name": "Temple of Tooth to Railway Station",
        "origin": "Sri Dalada Maligawa, Kandy, Sri Lanka",
        "destination": "Kandy Railway Station, Sri Lanka",
    },
]

CSV_FILE = "kandy_od_traffic.csv"

CSV_HEADERS = [
    "timestamp",
    "od_pair",
    "origin",
    "destination",
    "time_min",
    "distance_km",
    "avg_speed_kmh",
    "process_time_sec",
    "status",
]

# ---------------- PARSERS ----------------

def parse_time_minutes(text):
    hours = re.search(r"(\d+)\s*h", text)
    mins = re.search(r"(\d+)\s*min", text)

    total = 0
    if hours:
        total += int(hours.group(1)) * 60
    if mins:
        total += int(mins.group(1))

    return total if total > 0 else None


def parse_distance_km(text):
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
                await page.wait_for_timeout(2000)


async def scrape_route(page, od):
    start = time.time()

    result = {
        "timestamp": datetime.utcnow().isoformat(),
        "od_pair": od["name"],
        "origin": od["origin"],
        "destination": od["destination"],
        "time_min": None,
        "distance_km": None,
        "avg_speed_kmh": None,
        "process_time_sec": 0,
        "status": "failed",
    }

    try:
        url = f"https://www.google.com/maps/dir/{od['origin']}/{od['destination']}/"
        print(f"\nScraping {od['name']}")

        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await handle_consent(page)

        await page.wait_for_selector("div[role='main']", timeout=60000)
        await page.wait_for_timeout(3000)

        # Force route selection (CRITICAL)
        btns = page.locator("div[role='button']:has-text('min')")
        if await btns.count() > 0:
            await btns.first.click()
            await page.wait_for_timeout(2000)

        content = await page.inner_text("div[role='main']")

        time_match = re.search(r"\d+\s*(?:h\s*)?\d*\s*min", content)
        dist_match = re.search(r"[\d.]+\s*(?:km|m)\b", content)

        if not time_match or not dist_match:
            raise ValueError("ETA or distance not found")

        result["time_min"] = parse_time_minutes(time_match.group(0))
        result["distance_km"] = parse_distance_km(dist_match.group(0))

        if result["time_min"] and result["distance_km"]:
            result["avg_speed_kmh"] = round(
                result["distance_km"] / (result["time_min"] / 60), 2
            )
            result["status"] = "success"
        else:
            result["status"] = "incomplete_data"

    except Exception as e:
        result["status"] = f"error: {str(e)[:40]}"

    result["process_time_sec"] = round(time.time() - start, 2)
    print(f"Status: {result['status']}")

    return result

# ---------------- MAIN ----------------

async def main():
    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,   # REQUIRED
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
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

    print(f"\nSaved {len(results)} rows to {CSV_FILE}")

if __name__ == "__main__":
    asyncio.run(main())
