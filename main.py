#!/usr/bin/env python3
"""
Kandy Traffic Monitor
Debug mode – limited segmentation, JSON output
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

MAX_SEGMENTS_PER_ROUTE = 5   # 🔧 DEBUG LIMIT (5–10 recommended)
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
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2)**2
    return R * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))

def interpolate_segments(o, d, limit):
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

async def scrape_segment(context, page, route, seg_idx, seg):
    url = f"https://www.google.com/maps/dir/{seg[0]},{seg[1]}/{seg[2]},{seg[3]}/"
    log(f"[GoogleMaps] 🌐 {url}")

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

            dist_m = haversine(seg[0], seg[1], seg[2], seg[3])
            speed = round((dist_m / 1000) / (minutes / 60), 2)

            log(f"[Journey] ⏱ ETA={minutes} min | Speed={speed} km/h")

            return {
                "segment_index": seg_idx,
                "origin": [seg[0], seg[1]],
                "destination": [seg[2], seg[3]],
                "distance_m": round(dist_m, 2),
                "eta_min": minutes,
                "avg_speed_kmh": speed,
                "status": "success",
            }

        except Exception as e:
            log(f"❌ Attempt {attempt} failed: {e}")
            if "crashed" in str(e).lower():
                page = await context.new_page()

    return {
        "segment_index": seg_idx,
        "status": "failed",
    }

# ---------------- MAIN ----------------

async def main():
    now = datetime.utcnow()
    date_path = DATA_ROOT / now.strftime("%Y/%Y%m/%Y%m%d")
    date_path.mkdir(parents=True, exist_ok=True)

    outfile = date_path / f"{now.strftime('%Y%m%d.%H%M%S')}.json"

    all_results = {
        "timestamp_utc": now.isoformat(),
        "routes": {},
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(locale="en-US")
        page = await context.new_page()

        for route in ROUTES:
            log(f"🚗 Route: {route['name']}")
            segments = interpolate_segments(
                route["origin"],
                route["destination"],
                MAX_SEGMENTS_PER_ROUTE,
            )

            route_results = []
            for i, seg in enumerate(segments, 1):
                log(f"[{route['name']}] Segment {i}/{len(segments)}")
                res = await scrape_segment(context, page, route, i, seg)
                route_results.append(res)
                await asyncio.sleep(THROTTLE_SEC)

            all_results["routes"][route["name"]] = route_results

        await browser.close()

    outfile.write_text(json.dumps(all_results, indent=2))
    log(f"[Saved] {outfile} ({outfile.stat().st_size} B)")

if __name__ == "__main__":
    asyncio.run(main())
