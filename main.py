import asyncio
import csv
import os
import re
import time
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# Kandy OD Pairs
OD_PAIRS = [
    {"name": "Peradeniya_to_KMTT", "origin": "Peradeniya, Kandy", "dest": "KMTT, Kandy"},
    {"name": "Katugastota_to_KMTT", "origin": "Katugastota, Kandy", "dest": "KMTT, Kandy"},
    {"name": "KMTT_to_Getambe", "origin": "KMTT, Kandy", "dest": "Getambe, Kandy"}
]

CSV_FILE = "kandy_od_traffic.csv"
CSV_HEADERS = ["timestamp", "od_pair", "time_min", "distance_km", "avg_speed_kmh", "status"]

async def scrape_route(page, od_pair):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    result = {"timestamp": timestamp, "od_pair": od_pair["name"], "status": "failed"}
    
    try:
        # Standard Directions URL (More stable than bypass URLs)
        url = f"https://www.google.com/maps/dir/{od_pair['origin']}/{od_pair['dest']}/"
        await page.goto(url, wait_until="networkidle", timeout=60000)

        # Handle potential 'Consent' popups automatically
        if "consent" in page.url:
            await page.get_by_role("button", name="Accept all").click()
            await page.wait_for_load_state("networkidle")

        # Stable Selector: The first route result card
        # We look for the container that has the text 'min' or 'hr'
        route_card = page.locator('div[id="section-directions-trip-0"]').first
        await route_card.wait_for(state="visible", timeout=20000)
        
        # Extract the content of the card
        info_text = await route_card.inner_text()
        
        # Robust Parsing using Regex
        time_match = re.search(r'(\d+)\s*(min|hr|h)', info_text)
        dist_match = re.search(r'([\d.]+)\s*(km|m)', info_text)

        if time_match and dist_match:
            # Time logic (handles hours and minutes)
            raw_time = time_match.group(0).lower()
            mins = int(re.search(r'\d+', raw_time).group())
            if 'h' in raw_time: mins *= 60
            
            # Distance logic
            kms = float(dist_match.group(1))
            if 'm' in dist_match.group(2) and 'k' not in dist_match.group(2):
                kms /= 1000

            result.update({
                "time_min": mins,
                "distance_km": kms,
                "avg_speed_kmh": round(kms / (mins / 60), 2),
                "status": "success"
            })
            print(f"✅ {od_pair['name']}: {mins} mins, {kms} km")
        
    except Exception as e:
        print(f"❌ Error on {od_pair['name']}: {str(e)[:50]}")
    
    return result

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        
        all_results = []
        for pair in OD_PAIRS:
            res = await scrape_route(page, pair)
            all_results.append(res)
            await asyncio.sleep(2) # Politeness delay
        
        await browser.close()

        # Save results
        file_exists = os.path.isfile(CSV_FILE)
        df_cols = CSV_HEADERS
        with open(CSV_FILE, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=df_cols)
            if not file_exists: writer.writeheader()
            for r in all_results:
                # Filter to only write keys present in CSV_HEADERS
                row = {k: r.get(k, "") for k in df_cols}
                writer.writerow(row)

if __name__ == "__main__":
    asyncio.run(main())
