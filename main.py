#!/usr/bin/env python3
"""
Kandy Traffic Multi-Modal ETA Scraper
- Extracts Driving, Walking, Bicycling, Transit, Two-wheeler ETAs from Google Maps
- Robust: waits for buttons, retries on errors
- Debug mode: limits to first 5 segments per route
"""

import asyncio
import json
import math
import time
from pathlib import Path
from datetime import datetime
from playwright.async_api import async_playwright

# ---------------- CONFIG ----------------
MAX_SEGMENTS_PER_ROUTE = 5      # Debug limit, increase later
MAX_RETRIES = 3                 # Retry failed segment
THROTTLE_SEC = 1                # Delay between segments
DATA_ROOT = Path("data/journeys")
DATA_ROOT.mkdir(parents=True, exist_ok=True)

ROUTES = [
    {"name": "Peradeniya-to-KMTT", "origin": (6.895575, 79.854851), "destination": (6.871813, 79.884564)},
    {"name": "Temple-to-Railway", "origin": (6.9271, 79.8612), "destination": (6.9619, 79.8823)},
]

# ---------------- UTILITIES ----------------
def log(msg):
    ts = datetime.utcnow().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def haversine(lat1, lon1, lat2, lon2):
    """Distance between two coords in meters"""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return R * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))

def interpolate_segments(origin, destination, limit):
    """Split route into segments"""
    segments = []
    for i in range(limit):
        segments.append((
            origin[0] + (destination[0] - origin[0]) * i / limit,
            origin[1] + (destination[1] - origin[1]) * i / limit,
            origin[0] + (destination[0] - origin[0]) * (i + 1) / limit,
            origin[1] + (destination[1] - origin[1]) * (i + 1) / limit,
        ))
    return segments

# ---------------- SCRAPER ----------------
async def scrape_segment(page, route_name, seg_idx, seg):
    url = f"https://www.google.com/maps/dir/{seg[0]},{seg[1]}/{seg[2]},{seg[3]}/"
    log(f"[{route_name}] Segment {seg_idx} → {url}")

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=180000)
            await page.wait_for_selector("div[role='main']", timeout=60000)
            await page.wait_for_timeout(2000)

            # Wait for travel mode buttons to appear (up to 30s)
            timeout_start = time.time()
            buttons = await page.locator("button.m6Uuef").all()
            while len(buttons) < 1 and time.time() - timeout_start < 30:
                await asyncio.sleep(1)
                buttons = await page.locator("button.m6Uuef").all()

            if not buttons:
                raise ValueError("No travel mode buttons found")

            # Extract all travel modes from same page
            result = {
                "segment_index": seg_idx,
                "origin": [seg[0], seg[1]],
                "destination": [seg[2], seg[3]],
                "distance_m": round(haversine(seg[0], seg[1], seg[2], seg[3]), 2),
                "travel_modes": {},
                "status": "success",
            }

            for b in buttons:
                mode = await b.get_attribute("data-tooltip")
                eta_text = await b.locator("div.Fl2iee.HNPWFe").inner_text()

                # Convert ETA to minutes
                h = 0
                m = 0
                import re
                hh = re.search(r"(\d+)\s*h", eta_text)
                mm = re.search(r"(\d+)\s*min", eta_text)
                if hh: h = int(hh.group(1))
                if mm: m = int(mm.group(1))
                total_min = h * 60 + m

                result["travel_modes"][mode.lower()] = total_min
                log(f"[{mode}] ⏱ {total_min} min")

            return result

        except Exception as e:
            log(f"❌ Attempt {attempt} failed: {e}")
            await asyncio.sleep(2)
            if attempt == MAX_RETRIES:
                return {"segment_index": seg_idx, "status": f"failed: {str(e)[:40]}"}

# ---------------- MAIN ----------------
async def main():
    now = datetime.utcnow()
    outfile = DATA_ROOT / f"{now.strftime('%Y%m%d_%H%M%S')}.json"

    results = {"timestamp_utc": now.isoformat(), "routes": {}}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = await browser.new_context(viewport={"width":1920,"height":1080}, locale="en-US")
        page = await context.new_page()

        for route in ROUTES:
            log(f"🚗 Route: {route['name']}")
            segments = interpolate_segments(route["origin"], route["destination"], MAX_SEGMENTS_PER_ROUTE)
            route_results = []

            for i, seg in enumerate(segments, 1):
                res = await scrape_segment(page, route["name"], i, seg)
                route_results.append(res)
                await asyncio.sleep(THROTTLE_SEC)

            results["routes"][route["name"]] = route_results

        await browser.close()

    outfile.write_text(json.dumps(results, indent=2))
    log(f"[Saved] {outfile} ({outfile.stat().st_size} B)")

if __name__ == "__main__":
    asyncio.run(main())
