#!/usr/bin/env python3
"""
Kandy Traffic Monitor - Multi-modal ETA Scraper
Debug mode: 5 segments per route
Extracts all modes from a single URL per segment.
"""

import asyncio
import json
import math
import re
import time
from pathlib import Path
from datetime import datetime
from playwright.async_api import async_playwright

# ---------------- CONFIG ----------------

MAX_SEGMENTS_PER_ROUTE = 5  # Debug: limit segments
THROTTLE_SEC = 1
MAX_RETRIES = 3
SCREENSHOT_DIR = Path("screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

ROUTES = [
    {"name": "Peradeniya-to-KMTT", "origin": (6.895575, 79.854851), "destination": (6.871813, 79.884564)},
    {"name": "Temple-to-Railway", "origin": (6.9271, 79.8612), "destination": (6.9619, 79.8823)},
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

async def scrape_segment(context, page, route_name, seg_idx, seg):
    url = f"https://www.google.com/maps/dir/{seg[0]},{seg[1]}/{seg[2]},{seg[3]}/"
    log(f"[GoogleMaps] 🌐 {url}")

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_selector("button.m6Uuef", timeout=90000)
            await page.wait_for_timeout(2000)

            buttons = page.locator("button.m6Uuef")
            count = await buttons.count()
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
                "status": "failed",
            }

            for i in range(count):
                b = buttons.nth(i)
                mode = await b.get_attribute("data-tooltip")
                eta_text = await b.locator("div.Fl2iee.HNPWFe").inner_text()
                
                total_min = 0
                h = re.search(r"(\d+)\s*h", eta_text)
                m = re.search(r"(\d+)\s*min", eta_text)
                if h: total_min += int(h.group(1)) * 60
                if m: total_min += int(m.group(1))

                mode_lower = mode.lower() if mode else ""
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

            result["status"] = "success"
            return result

        except Exception as e:
            log(f"❌ Attempt {attempt} failed: {e}")
            if attempt == MAX_RETRIES:
                screenshot = SCREENSHOT_DIR / f"{route_name}_seg{seg_idx}.png"
                await page.screenshot(path=screenshot)
            else:
                log(f"⏳ Retrying after delay...")
                await asyncio.sleep(3)

    return {"segment_index": seg_idx, "status": "failed"}

# ---------------- MAIN ----------------

async def main():
    now = datetime.utcnow()
    results = {
        "timestamp_utc": now.isoformat(),
        "routes": {}
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = await browser.new_context(locale="en-US")
        page = await context.new_page()

        for route in ROUTES:
            log(f"🚗 Route: {route['name']}")
            segments = interpolate_segments(route["origin"], route["destination"], MAX_SEGMENTS_PER_ROUTE)
            route_results = []

            for i, seg in enumerate(segments, 1):
                log(f"[{route['name']}] Segment {i}/{len(segments)}")
                res = await scrape_segment(context, page, route["name"], i, seg)
                route_results.append(res)
                await asyncio.sleep(THROTTLE_SEC)

            results["routes"][route["name"]] = route_results

        await browser.close()

    # Save results as JSON
    out_file = Path(f"kandy_multi_mode_{now.strftime('%Y%m%d_%H%M%S')}.json")
    out_file.write_text(json.dumps(results, indent=2))
    log(f"[Saved] {out_file} ({out_file.stat().st_size} B)")

if __name__ == "__main__":
    asyncio.run(main())
