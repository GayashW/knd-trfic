#!/usr/bin/env python3
"""
Kandy Traffic Segment Monitor - Multi-modal ETA
Generates road segments (debug 5 segments) and scrapes Google Maps ETAs for driving, walking, bicycling, transit, and two-wheeler.
"""

import asyncio
import csv
import math
import time
from pathlib import Path
from datetime import datetime

from playwright.async_api import async_playwright

# ---------------- CONFIG ----------------

SEGMENTS_FILE = Path("kandy_segments.csv")
OUTPUT_FILE = Path("kandy_segment_speeds.csv")
SCREENSHOT_DIR = Path("screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

MAX_SEGMENT_LENGTH_M = 10  # debug: 10m segments
MAX_RETRIES = 2

ROUTES = [
    {"name": "Peradeniya-to-KMTT", "origin": (6.895575, 79.854851), "destination": (6.871813, 79.884564)},
    {"name": "Temple-to-Railway", "origin": (6.9271, 79.8612), "destination": (6.9619, 79.8823)},
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
    "driving_min",
    "walking_min",
    "bicycling_min",
    "transit_min",
    "two_wheeler_min",
    "process_time_sec",
    "status",
]

# ---------------- UTILITIES ----------------

def haversine(lat1, lon1, lat2, lon2):
    """Distance between two coords in meters"""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(d_lambda/2)**2
    return R * c

def interpolate_segments(lat1, lon1, lat2, lon2, max_len_m=10):
    """Split line into multiple segments with max length"""
    dist = haversine(lat1, lon1, lat2, lon2)
    num = max(1, math.ceil(dist / max_len_m))
    segments = []
    for i in range(num):
        start_lat = lat1 + (lat2 - lat1) * i / num
        start_lon = lon1 + (lon2 - lon1) * i / num
        end_lat = lat1 + (lat2 - lat1) * (i+1) / num
        end_lon = lon1 + (lon2 - lon1) * (i+1) / num
        segments.append((start_lat, start_lon, end_lat, end_lon))
    return segments

def generate_segments(routes):
    all_segments = []
    for route in routes:
        segments = interpolate_segments(route["origin"][0], route["origin"][1], route["destination"][0], route["destination"][1], MAX_SEGMENT_LENGTH_M)
        for idx, (olat, olon, dlat, dlon) in enumerate(segments):
            seg_id = f"{route['name'].replace(' ', '_')}_seg_{idx+1}"
            all_segments.append({
                "segment_id": seg_id,
                "od_pair": route["name"],
                "origin_lat": olat,
                "origin_lng": olon,
                "destination_lat": dlat,
                "destination_lng": dlon,
                "distance_m": round(haversine(olat, olon, dlat, dlon), 2),
            })
    return all_segments

# Save or load segments
if not SEGMENTS_FILE.exists():
    segments = generate_segments(ROUTES)
    with open(SEGMENTS_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(segments[0].keys()))
        writer.writeheader()
        for seg in segments:
            writer.writerow(seg)
    print(f"[INFO] Generated {len(segments)} segments → {SEGMENTS_FILE}")
else:
    print(f"[INFO] Using existing segments → {SEGMENTS_FILE}")
    segments = []
    with open(SEGMENTS_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            for k in ["origin_lat","origin_lng","destination_lat","destination_lng","distance_m"]:
                row[k] = float(row[k])
            segments.append(row)

# ---------------- PLAYWRIGHT SCRAPER ----------------

async def handle_consent(page):
    for frame in page.frames:
        if "consent.google.com" in frame.url:
            btn = frame.locator("button:has-text('Accept all')")
            if await btn.count() > 0:
                await btn.first.click()
                await page.wait_for_timeout(1500)

async def scrape_segment(page, segment):
    start_time = time.time()
    result = {
        "timestamp_utc": datetime.utcnow().isoformat(),
        "segment_id": segment["segment_id"],
        "od_pair": segment["od_pair"],
        "origin_lat": segment["origin_lat"],
        "origin_lng": segment["origin_lng"],
        "destination_lat": segment["destination_lat"],
        "destination_lng": segment["destination_lng"],
        "distance_m": segment["distance_m"],
        "driving_min": None,
        "walking_min": None,
        "bicycling_min": None,
        "transit_min": None,
        "two_wheeler_min": None,
        "process_time_sec": 0,
        "status": "failed",
    }

    url = f"https://www.google.com/maps/dir/{segment['origin_lat']},{segment['origin_lng']}/{segment['destination_lat']},{segment['destination_lng']}/"

    for attempt in range(1, MAX_RETRIES+1):
        try:
            print(f"[{segment['od_pair']}] Segment {segment['segment_id']} → Loading page")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await handle_consent(page)
            await page.wait_for_selector("div[role='main']", timeout=60000)
            await page.wait_for_timeout(2000)

            # --- extract multi-modal ETAs from buttons ---
            buttons = page.locator("button.m6Uuef")
            count = await buttons.count()
            for i in range(count):
                b = buttons.nth(i)
                mode = await b.get_attribute("data-tooltip")
                eta_text = await b.locator("div.Fl2iee.HNPWFe").inner_text()

                import re
                total_min = 0
                h = re.search(r"(\d+)\s*h", eta_text)
                m = re.search(r"(\d+)\s*min", eta_text)
                if h: total_min += int(h.group(1)) * 60
                if m: total_min += int(m.group(1))

                if mode.lower() == "driving":
                    result["driving_min"] = total_min
                elif mode.lower() == "walking":
                    result["walking_min"] = total_min
                elif mode.lower() == "bicycling":
                    result["bicycling_min"] = total_min
                elif mode.lower() == "transit":
                    result["transit_min"] = total_min
                elif mode.lower() in ["two-wheeler", "motorbike"]:
                    result["two_wheeler_min"] = total_min

                print(f"[GoogleMaps] {mode} → {url}")
                print(f"[{mode.lower()}] ⏱ ETA={total_min} min")

            result["status"] = "success"
            break

        except Exception as e:
            print(f"  ❌ {segment['segment_id']} attempt {attempt} failed: {e}")
            if attempt == MAX_RETRIES:
                screenshot = SCREENSHOT_DIR / f"{segment['segment_id']}.png"
                await page.screenshot(path=screenshot)
                result["status"] = f"error: {str(e)[:40]}"

    result["process_time_sec"] = round(time.time() - start_time, 2)
    return result

# ---------------- MAIN ----------------

async def main():
    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"
        ])
        context = await browser.new_context(viewport={"width":1280,"height":800}, locale="en-US", timezone_id="Asia/Colombo")
        page = await context.new_page()

        # debug: limit to first 5 segments
        for segment in segments[:5]:
            res = await scrape_segment(page, segment)
            results.append(res)
            await asyncio.sleep(1)

        await browser.close()

    # save results
    file_exists = OUTPUT_FILE.exists()
    with open(OUTPUT_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        if not file_exists:
            writer.writeheader()
        for r in results:
            writer.writerow(r)

    print(f"[INFO] Saved {len(results)} rows → {OUTPUT_FILE}")

if __name__ == "__main__":
    asyncio.run(main())
