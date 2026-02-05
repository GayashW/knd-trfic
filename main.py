#!/usr/bin/env python3
"""
Kandy Traffic Segment Monitor
Generates road segments (max 5m) and runs virtual car scraper
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

MAX_SEGMENT_LENGTH_M = 5  # Maximum length per segment
MAX_RETRIES = 2

# Define routes roughly (start & end coords)
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

def haversine(lat1, lon1, lat2, lon2):
    """Distance between two coords in meters"""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(d_lambda/2)**2
    c = 2*math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

def interpolate_segments(lat1, lon1, lat2, lon2, max_len_m=5):
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

# ---------------- SEGMENT GENERATION ----------------

def generate_segments(routes):
    all_segments = []
    for route in routes:
        segments = interpolate_segments(
            route["origin"][0],
            route["origin"][1],
            route["destination"][0],
            route["destination"][1],
            max_len_m=MAX_SEGMENT_LENGTH_M,
        )
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

# Save segments once
if not SEGMENTS_FILE.exists():
    segments = generate_segments(ROUTES)
    with open(SEGMENTS_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(segments[0].keys()))
        writer.writeheader()
        for seg in segments:
            writer.writerow(seg)
    print(f"Saved {len(segments)} segments → {SEGMENTS_FILE}")
else:
    print(f"Using existing segments → {SEGMENTS_FILE}")
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
        "time_min": None,
        "avg_speed_kmh": None,
        "process_time_sec": 0,
        "status": "failed",
    }

    url = f"https://www.google.com/maps/dir/{segment['origin_lat']},{segment['origin_lng']}/{segment['destination_lat']},{segment['destination_lng']}/"

    for attempt in range(1, MAX_RETRIES+1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await handle_consent(page)
            await page.wait_for_selector("div[role='main']", timeout=60000)
            await page.wait_for_timeout(2000)

            content = await page.inner_text("div[role='main']")

            import re
            t_match = re.search(r"\d+\s*(?:h\s*)?\d*\s*min", content)
            if not t_match:
                raise ValueError("ETA not found")

            # parse time in minutes
            text = t_match.group(0)
            h = re.search(r"(\d+)\s*h", text)
            m = re.search(r"(\d+)\s*min", text)
            total_min = 0
            if h: total_min += int(h.group(1))*60
            if m: total_min += int(m.group(1))
            if total_min == 0:
                raise ValueError("ETA parsed as 0")

            result["time_min"] = total_min
            result["avg_speed_kmh"] = round((segment["distance_m"]/1000)/(total_min/60), 2)
            result["status"] = "success"
            break

        except Exception as e:
            print(f"  ❌ {segment['segment_id']} attempt {attempt}: {e}")
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

        for segment in segments:
            res = await scrape_segment(page, segment)
            results.append(res)
            await asyncio.sleep(1)  # throttle to avoid blocking

        await browser.close()

    # save results
    file_exists = OUTPUT_FILE.exists()
    with open(OUTPUT_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        if not file_exists:
            writer.writeheader()
        for r in results:
            writer.writerow(r)

    print(f"Saved {len(results)} rows → {OUTPUT_FILE}")

if __name__ == "__main__":
    asyncio.run(main())
