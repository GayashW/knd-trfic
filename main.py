#!/usr/bin/env python3
"""
Kandy Traffic Monitor - Multi-modal ETA extraction (single URL per segment)
Debug mode – limited segments
"""

import asyncio
import json
import math
import time
import re
from pathlib import Path
from datetime import datetime
from playwright.async_api import async_playwright

# ---------------- CONFIG ----------------

MAX_SEGMENTS_PER_ROUTE = 5  # Debug limit
THROTTLE_SEC = 1
MAX_RETRIES = 2
DATA_ROOT = Path("data/journeys")

ROUTES = [
    {
        "name": "Peradeniya-to-KMTT",
        "origin": (6.895575, 79.854851),
        "destination": (6.871813, 79.884564),
    },
    {
        "name": "Temple-to-Railway",
        "origin": (6.9271, 79.8612),
        "destination": (6.9619, 79.8823),
    },
]

# ---------------- LOGGING ----------------

def log(msg):
    ts = datetime.utcnow().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# ---------------- GEO ----------------

def haversine(lat1, lon1, lat2, lon2):
    """Distance between two points in meters"""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda/2)**2
    return R * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))

def interpolate_segments(o, d, limit):
    """Split origin→destination into `limit` segments"""
    segs = []
    for i in range(limit):
        segs.append((
            o[0] + (d[0] - o[0]) * i / limit,
            o[1] + (d[1] - o[1]) * i / limit,
            o[0] + (d[0] - o[0]) * (i + 1) / limit,
            o[1] + (d[1] - o[1]) * (i + 1) / limit,
        ))
    return segs

# ---------------- SCRAPER ----------------

async def scrape_segment(page, route_name, seg_idx, seg):
    url = f"https://www.google.com/maps/dir/{seg[0]},{seg[1]}/{seg[2]},{seg[3]}/"
    log(f"[GoogleMaps] 🌐 {url}")

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await page.goto(url, wait_until="networkidle", timeout=180000)
            # Dynamic wait until buttons appear
            timeout_start = time.time()
            buttons = await page.locator("button.m6Uuef").all()
            while len(buttons) < 3 and (time.time() - timeout_start) < 180:
                await asyncio.sleep(1)
                buttons = await page.locator("button.m6Uuef").all()

            if len(buttons) == 0:
                raise ValueError("No mode buttons found")

            # Extract all ETAs
            result = {
                "segment_index": seg_idx,
                "origin": [seg[0], seg[1]],
                "destination": [seg[2], seg[3]],
                "distance_m": round(haversine(seg[0], seg[1], seg[2], seg[3]), 2),
                "driving_min": None,
                "walking_min": None,
                "bicycling_min": None,
                "transit_min": None,
                "two_wheeler_min": None,
                "status": "success",
            }

            for b in buttons:
                mode = await b.get_attribute("data-tooltip")
                eta_text = await b.locator("div.Fl2iee.HNPWFe").inner_text()

                total_min = 0
                h = re.search(r"(\d+)\s*h", eta_text)
                m = re.search(r"(\d+)\s*min", eta_text)
                if h: total_min += int(h.group(1)) * 60
                if m: total_min += int(m.group(1))

                if mode:
                    mode_lower = mode.lower()
                    if mode_lower == "driving":
                        result["driving_min"] = total_min
                    elif mode_lower == "walking":
                        result["walking_min"] = total_min
                    elif mode_lower == "bicycling":
                        result["bicycling_min"] = total_min
                    elif mode_lower == "transit":
                        result["transit_min"] = total_min
                    elif mode_lower in ["two-wheeler", "motorbike"]:
                        result["two_wheeler_min"] = total_min

                    log(f"[{mode}] ⏱ ETA={total_min} min")

            return result

        except Exception as e:
            log(f"❌ Attempt {attempt} failed: {e}")
            if attempt < MAX_RETRIES:
                log("⏳ Retrying after delay...")
                await asyncio.sleep(5)
            else:
                return {"segment_index": seg_idx, "status": f"failed: {e}"}

# ---------------- MAIN ----------------

async def main():
    now = datetime.utcnow()
    date_path = DATA_ROOT / now.strftime("%Y/%Y%m/%Y%m%d")
    date_path.mkdir(parents=True, exist_ok=True)
    outfile = date_path / f"{now.strftime('%Y%m%d.%H%M%S')}.json"

    all_results = {"timestamp_utc": now.isoformat(), "routes": {}}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(viewport={"width":1920,"height":1080}, locale="en-US")
        page = await context.new_page()

        for route in ROUTES:
            log(f"🚗 Route: {route['name']}")
            segments = interpolate_segments(route["origin"], route["destination"], MAX_SEGMENTS_PER_ROUTE)

            route_results = []
            for i, seg in enumerate(segments, 1):
                log(f"[{route['name']}] Segment {i}/{len(segments)}")
                res = await scrape_segment(page, route["name"], i, seg)
                route_results.append(res)
                await asyncio.sleep(THROTTLE_SEC)

            all_results["routes"][route["name"]] = route_results

        await browser.close()

    outfile.write_text(json.dumps(all_results, indent=2))
    log(f"[Saved] {outfile} ({outfile.stat().st_size} B)")

if __name__ == "__main__":
    asyncio.run(main())
