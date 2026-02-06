#!/usr/bin/env python3
"""
Kandy Traffic Segment Monitor (DEBUG MODE)
"""

import asyncio
import csv
import math
import time
import re
from pathlib import Path
from datetime import datetime

from playwright.async_api import async_playwright, Error as PlaywrightError

# ---------------- CONFIG ----------------

SEGMENTS_FILE = Path("kandy_segments.csv")
OUTPUT_FILE = Path("kandy_segment_speeds.csv")
SCREENSHOT_DIR = Path("screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

MAX_SEGMENT_LENGTH_M = 100   # 🔧 DEBUG: reduced workload
MAX_RETRIES = 2
DEBUG = True

ROUTES = [
    {
        "name": "Peradeniya → KMTT",
        "origin": (6.895575, 79.854851),
        "destination": (6.871813, 79.884564),
    },
    {
        "name": "Temple → Railway",
        "origin": (6.9271, 79.8612),
        "destination": (6.9619, 79.8823),
    },
]

CSV_HEADERS = [
    "timestamp_utc",
    "segment_id",
    "od_pair",
    "origin_lat",
    "origin_lng",
    "destination_lat",
    "destination_lng",
    "distance_m",
    "time_min",
    "avg_speed_kmh",
    "process_time_sec",
    "status",
]

# ---------------- UTILITIES ----------------

def log(msg):
    ts = datetime.utcnow().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(d_lambda/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def interpolate_segments(lat1, lon1, lat2, lon2, max_len_m):
    dist = haversine(lat1, lon1, lat2, lon2)
    num = max(1, math.ceil(dist / max_len_m))
    segments = []
    for i in range(num):
        segments.append((
            lat1 + (lat2 - lat1) * i / num,
            lon1 + (lon2 - lon1) * i / num,
            lat1 + (lat2 - lat1) * (i + 1) / num,
            lon1 + (lon2 - lon1) * (i + 1) / num,
        ))
    return segments

# ---------------- SEGMENT GENERATION ----------------

def generate_segments(routes):
    all_segments = []
    for route in routes:
        segs = interpolate_segments(
            route["origin"][0],
            route["origin"][1],
            route["destination"][0],
            route["destination"][1],
            MAX_SEGMENT_LENGTH_M,
        )
        for i, (olat, olon, dlat, dlon) in enumerate(segs):
            all_segments.append({
                "segment_id": f"{route['name'].replace(' ', '_')}_seg_{i+1}",
                "od_pair": route["name"],
                "origin_lat": olat,
                "origin_lng": olon,
                "destination_lat": dlat,
                "destination_lng": dlon,
                "distance_m": round(haversine(olat, olon, dlat, dlon), 2),
            })
    return all_segments

if not SEGMENTS_FILE.exists():
    segments = generate_segments(ROUTES)
    with open(SEGMENTS_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=segments[0].keys())
        writer.writeheader()
        writer.writerows(segments)
    log(f"Generated {len(segments)} segments")
else:
    log("Loading segments from CSV")
    segments = []
    with open(SEGMENTS_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            for k in ["origin_lat", "origin_lng", "destination_lat", "destination_lng", "distance_m"]:
                r[k] = float(r[k])
            segments.append(r)
    log(f"Loaded {len(segments)} segments")

# ---------------- SCRAPER ----------------

async def handle_consent(page):
    for frame in page.frames:
        if "consent.google.com" in frame.url:
            btn = frame.locator("button:has-text('Accept all')")
            if await btn.count() > 0:
                await btn.first.click()
                await page.wait_for_timeout(1500)

async def scrape_segment(context, page, segment, index, total):
    log(f"[{index}/{total}] {segment['segment_id']} START")
    t0 = time.time()

    url = f"https://www.google.com/maps/dir/{segment['origin_lat']},{segment['origin_lng']}/{segment['destination_lat']},{segment['destination_lng']}/"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log(f"  → Attempt {attempt} | goto()")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            await handle_consent(page)
            await page.wait_for_selector("div[role='main']", timeout=60000)
            await page.wait_for_timeout(2000)

            content = await page.inner_text("div[role='main']")
            m = re.search(r"\d+\s*(?:h\s*)?\d*\s*min", content)
            if not m:
                raise ValueError("ETA not found")

            text = m.group(0)
            h = re.search(r"(\d+)\s*h", text)
            m_ = re.search(r"(\d+)\s*min", text)

            minutes = (int(h.group(1)) * 60 if h else 0) + (int(m_.group(1)) if m_ else 0)
            if minutes == 0:
                raise ValueError("Parsed ETA = 0")

            speed = round((segment["distance_m"] / 1000) / (minutes / 60), 2)

            log(f"  ✔ ETA={minutes} min | speed={speed} km/h")

            return {
                **segment,
                "timestamp_utc": datetime.utcnow().isoformat(),
                "time_min": minutes,
                "avg_speed_kmh": speed,
                "process_time_sec": round(time.time() - t0, 2),
                "status": "success",
            }

        except Exception as e:
            log(f"  ❌ Attempt {attempt} failed: {e}")

            if "crashed" in str(e).lower():
                log("  🔄 Page crashed → recreating page")
                try:
                    await page.close()
                except:
                    pass
                page = await context.new_page()

            if attempt == MAX_RETRIES:
                return {
                    **segment,
                    "timestamp_utc": datetime.utcnow().isoformat(),
                    "time_min": None,
                    "avg_speed_kmh": None,
                    "process_time_sec": round(time.time() - t0, 2),
                    "status": f"error: {str(e)[:50]}",
                }

# ---------------- MAIN ----------------

async def main():
    results = []

    async with async_playwright() as p:
        log("Launching Chromium")
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-software-rasterizer",
            ],
        )

        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="Asia/Colombo",
        )

        page = await context.new_page()

        total = len(segments)
        for i, seg in enumerate(segments, 1):
            res = await scrape_segment(context, page, seg, i, total)
            results.append(res)
            await asyncio.sleep(1)

        await browser.close()

    log("Writing output CSV")
    exists = OUTPUT_FILE.exists()
    with open(OUTPUT_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        if not exists:
            w.writeheader()
        w.writerows(results)

    log(f"Saved {len(results)} rows")

if __name__ == "__main__":
    asyncio.run(main())
