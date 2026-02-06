#!/usr/bin/env python3
"""
Kandy Traffic Monitor
Multi-modal ETA scraping
Debug mode – limited segmentation (5 per route)
Waypoint-locked URLs to keep same route every time
"""

import asyncio
import json
import math
import time
import re
from pathlib import Path
from datetime import datetime
from itertools import pairwise  # Python 3.10+

from playwright.async_api import async_playwright

# ---------------- CONFIG ----------------

MAX_SEGMENTS_PER_ROUTE = 5  # Debug: limit segments
THROTTLE_SEC = 1
MAX_RETRIES = 2

DATA_ROOT = Path("data/journeys")

# ---------------- ROUTES ----------------
# Each route has a set of waypoints to lock the path
ROUTES = [
    {
        "name": "Peradeniya-to-KMTT",
        "waypoints": [
            (6.895575, 79.854851),
            (6.892401, 79.858942),
            (6.889102, 79.865433),
            (6.884221, 79.872901),
            (6.871813, 79.884564),
        ],
    },
    {
        "name": "Temple-to-Railway",
        "waypoints": [
            (6.9271, 79.8612),
            (6.9352, 79.8671),
            (6.9443, 79.8720),
            (6.9532, 79.8765),
            (6.9619, 79.8823),
        ],
    },
]

TRAVEL_MODES = {
    "driving": "🚗",
    "walking": "🚶",
    "bicycling": "🚲",
    "transit": "🚌",
}

# ---------------- UTILITIES ----------------

def log(msg):
    ts = datetime.utcnow().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def haversine(lat1, lon1, lat2, lon2):
    """Distance in meters between two lat/lng"""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi/2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda/2)**2
    return R * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))

def split_segment(start, end, max_segments=5):
    """Split a line into multiple segments"""
    segments = []
    for i in range(max_segments):
        segments.append((
            start[0] + (end[0] - start[0]) * i / max_segments,
            start[1] + (end[1] - start[1]) * i / max_segments,
            start[0] + (end[0] - start[0]) * (i + 1) / max_segments,
            start[1] + (end[1] - start[1]) * (i + 1) / max_segments,
        ))
    return segments

def build_url(points, mode):
    """Build Google Maps URL with waypoints and travel mode"""
    path = "/".join(f"{lat},{lng}" for lat, lng in points)
    return f"https://www.google.com/maps/dir/{path}/?travelmode={mode}"

# ---------------- SCRAPER ----------------

async def scrape_segment(context, page, route_name, seg_idx, seg_points):
    results = {}
    for mode, emoji in TRAVEL_MODES.items():
        url = build_url(seg_points, mode)
        log(f"[GoogleMaps] {emoji} {mode} → {url}")

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_selector("div[role='main']", timeout=60000)
                await page.wait_for_timeout(2000)

                text = await page.inner_text("div[role='main']")
                m = re.search(r"\d+\s*(?:h\s*)?\d*\s*min", text)
                if not m:
                    raise ValueError("ETA not found")

                t = m.group(0)
                h = re.search(r"(\d+)\s*h", t)
                m2 = re.search(r"(\d+)\s*min", t)
                minutes = (int(h.group(1)) * 60 if h else 0) + (int(m2.group(1)) if m2 else 0)

                if minutes <= 0:
                    raise ValueError("Invalid ETA")

                results[mode] = {"eta_min": minutes, "status": "success"}

                # Driving gets distance and avg speed
                if mode == "driving":
                    dist_m = haversine(seg_points[0][0], seg_points[0][1], seg_points[-1][0], seg_points[-1][1])
                    results[mode]["distance_m"] = round(dist_m, 2)
                    results[mode]["avg_speed_kmh"] = round((dist_m / 1000) / (minutes / 60), 2)

                log(f"[{mode}] ⏱ ETA={minutes} min")
                break

            except Exception as e:
                log(f"❌ {mode} attempt {attempt} failed: {e}")
                if attempt == MAX_RETRIES:
                    results[mode] = {"status": "failed", "error": str(e)}

        await asyncio.sleep(THROTTLE_SEC)

    return {
        "segment_index": seg_idx,
        "origin": [seg_points[0][0], seg_points[0][1]],
        "destination": [seg_points[-1][0], seg_points[-1][1]],
        "modes": results,
    }

# ---------------- MAIN ----------------

async def main():
    now = datetime.utcnow()
    date_path = DATA_ROOT / now.strftime("%Y/%Y%m/%Y%m%d")
    date_path.mkdir(parents=True, exist_ok=True)
    outfile = date_path / f"{now.strftime('%Y%m%d.%H%M%S')}.json"

    all_results = {"timestamp_utc": now.isoformat(), "routes": {}}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = await browser.new_context(locale="en-US")
        page = await context.new_page()

        for route in ROUTES:
            route_name = route["name"]
            log(f"🚗 Route: {route_name}")
            waypoints = route["waypoints"]

            # Split route into segments
            segments = []
            for start, end in pairwise(waypoints):
                segments.extend(split_segment(start, end, MAX_SEGMENTS_PER_ROUTE))

            route_results = []
            for i, seg_points in enumerate(segments, 1):
                log(f"[{route_name}] Segment {i}/{len(segments)}")
                res = await scrape_segment(context, page, route_name, i, [seg_points[:2], seg_points[2:]])
                route_results.append(res)

            all_results["routes"][route_name] = route_results

        await browser.close()

    outfile.write_text(json.dumps(all_results, indent=2))
    log(f"[Saved] {outfile} ({outfile.stat().st_size} B)")

if __name__ == "__main__":
    asyncio.run(main())
