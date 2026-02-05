#!/usr/bin/env python3
"""
Virtual Floating Car Traffic Monitor for Kandy, Sri Lanka
Scrapes Google Maps Directions data to extract real-time traffic metrics
"""

import asyncio
import csv
import os
import re
import time
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# Origin-Destination pairs for Kandy region
OD_PAIRS = [
    {
        "name": "Peradeniya to KMTT",
        "origin": "Peradeniya, Sri Lanka",
        "destination": "Kandy Municipal Transport Terminal, Kandy, Sri Lanka"
    },
    {
        "name": "Temple of Tooth to Railway Station",
        "origin": "Sri Dalada Maligawa, Kandy, Sri Lanka",
        "destination": "Kandy Railway Station, Sri Lanka"
    },
    {
        "name": "Peradeniya University to City Center",
        "origin": "University of Peradeniya, Sri Lanka",
        "destination": "Kandy City Center, Sri Lanka"
    },
    {
        "name": "Katugastota to Peradeniya",
        "origin": "Katugastota, Sri Lanka",
        "destination": "Peradeniya, Sri Lanka"
    },
    {
        "name": "Kandy to Digana",
        "origin": "Kandy, Sri Lanka",
        "destination": "Digana, Sri Lanka"
    }
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
    "status"
]


def parse_time(time_str):
    """
    Parse time string from Google Maps (e.g., '15 min', '1 h 30 min')
    Returns time in minutes as float
    """
    if not time_str:
        return None
    
    time_str = time_str.lower().strip()
    total_minutes = 0
    
    # Match hours
    hour_match = re.search(r'(\d+)\s*h', time_str)
    if hour_match:
        total_minutes += int(hour_match.group(1)) * 60
    
    # Match minutes
    min_match = re.search(r'(\d+)\s*min', time_str)
    if min_match:
        total_minutes += int(min_match.group(1))
    
    return total_minutes if total_minutes > 0 else None


def parse_distance(dist_str):
    """
    Parse distance string from Google Maps (e.g., '5.2 km', '500 m')
    Returns distance in kilometers as float
    """
    if not dist_str:
        return None
    
    dist_str = dist_str.lower().strip()
    
    # Match kilometers
    km_match = re.search(r'([\d.]+)\s*km', dist_str)
    if km_match:
        return float(km_match.group(1))
    
    # Match meters
    m_match = re.search(r'([\d.]+)\s*m', dist_str)
    if m_match:
        return float(m_match.group(1)) / 1000
    
    return None


async def scrape_route(page, od_pair):
    """
    Scrape a single Origin-Destination route
    """
    start_time = time.time()
    result = {
        "timestamp": datetime.now().isoformat(),
        "od_pair": od_pair["name"],
        "origin": od_pair["origin"],
        "destination": od_pair["destination"],
        "time_min": None,
        "distance_km": None,
        "avg_speed_kmh": None,
        "process_time_sec": 0,
        "status": "failed"
    }
    
    try:
        # Construct Google Maps URL
        origin_encoded = od_pair["origin"].replace(" ", "+")
        dest_encoded = od_pair["destination"].replace(" ", "+")
        url = f"https://www.google.com/maps/dir/{origin_encoded}/{dest_encoded}/"
        
        print(f"\n{'='*60}")
        print(f"Scraping: {od_pair['name']}")
        print(f"URL: {url}")
        
        # Navigate to the page
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        
        # Handle cookie consent popup if present
        try:
            consent_button = page.locator('button:has-text("Accept all")')
            if await consent_button.count() > 0:
                print("Found consent popup, clicking 'Accept all'...")
                await consent_button.first.click(timeout=3000)
                await page.wait_for_timeout(2000)
        except Exception as e:
            print(f"No consent popup or error handling it: {e}")
        
        # Wait for directions panel to load
        try:
            await page.wait_for_selector(
                'div[jstcache="3"]',  # Directions container
                timeout=15000
            )
            await page.wait_for_timeout(2000)  # Extra wait for dynamic content
        except PlaywrightTimeout:
            print("Timeout waiting for directions panel")
            result["status"] = "timeout"
            return result
        
        # Extract time and distance from the first route option
        # Multiple selectors as Google Maps HTML structure can vary
        time_text = None
        distance_text = None
        
        # Strategy 1: Look for the first route summary
        try:
            # Find the best route (usually first in list)
            route_selector = 'div[jstcache="3"] div[jstcache] div[jstcache]'
            route_elements = page.locator(route_selector)
            
            if await route_elements.count() > 0:
                # Get text content from the route summary
                first_route = route_elements.first
                route_text = await first_route.inner_text()
                print(f"Route text: {route_text}")
                
                # Parse time and distance from the text
                lines = route_text.split('\n')
                for line in lines:
                    if 'min' in line.lower() or 'hour' in line.lower():
                        time_text = line
                    if 'km' in line.lower() or 'm' in line.lower():
                        distance_text = line
        except Exception as e:
            print(f"Strategy 1 failed: {e}")
        
        # Strategy 2: Alternative selectors
        if not time_text or not distance_text:
            try:
                # Look for specific aria-labels or data attributes
                all_text = await page.inner_text('div[id="section-directions-trip-0"]')
                print(f"Alternative text: {all_text}")
                
                lines = all_text.split('\n')
                for line in lines:
                    if not time_text and ('min' in line.lower() or 'hour' in line.lower()):
                        if re.search(r'\d+\s*(min|hour|h)', line.lower()):
                            time_text = line
                    if not distance_text and ('km' in line.lower() or 'm' in line.lower()):
                        if re.search(r'\d+\.?\d*\s*(km|m)', line.lower()):
                            distance_text = line
            except Exception as e:
                print(f"Strategy 2 failed: {e}")
        
        # Strategy 3: Use page content search
        if not time_text or not distance_text:
            try:
                page_content = await page.content()
                # Search for time pattern
                time_matches = re.findall(r'(\d+\s*(?:h|hour|min)(?:\s*\d+\s*min)?)', page_content, re.IGNORECASE)
                if time_matches and not time_text:
                    time_text = time_matches[0]
                
                # Search for distance pattern
                dist_matches = re.findall(r'([\d.]+\s*(?:km|m))', page_content, re.IGNORECASE)
                if dist_matches and not distance_text:
                    distance_text = dist_matches[0]
            except Exception as e:
                print(f"Strategy 3 failed: {e}")
        
        print(f"Extracted - Time: {time_text}, Distance: {distance_text}")
        
        # Parse the extracted data
        if time_text:
            result["time_min"] = parse_time(time_text)
        
        if distance_text:
            result["distance_km"] = parse_distance(distance_text)
        
        # Calculate average speed
        if result["time_min"] and result["distance_km"] and result["time_min"] > 0:
            result["avg_speed_kmh"] = round(
                result["distance_km"] / (result["time_min"] / 60),
                2
            )
            result["status"] = "success"
        else:
            result["status"] = "incomplete_data"
        
    except Exception as e:
        print(f"Error scraping {od_pair['name']}: {e}")
        result["status"] = f"error: {str(e)[:50]}"
    
    finally:
        result["process_time_sec"] = round(time.time() - start_time, 2)
        print(f"Status: {result['status']}")
        print(f"Process time: {result['process_time_sec']}s")
    
    return result


async def main():
    """
    Main scraping function
    """
    print("="*60)
    print("Kandy Virtual Floating Car Traffic Monitor")
    print(f"Starting scrape at {datetime.now().isoformat()}")
    print("="*60)
    
    results = []
    
    async with async_playwright() as p:
        # Launch browser with stealth settings
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled'
            ]
        )
        
        # Create context with realistic user agent
        context = await browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            locale='en-US',
            timezone_id='Asia/Colombo'
        )
        
        # Add stealth JavaScript
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)
        
        page = await context.new_page()
        
        # Scrape each OD pair
        for od_pair in OD_PAIRS:
            result = await scrape_route(page, od_pair)
            results.append(result)
            
            # Delay between requests to avoid rate limiting
            await asyncio.sleep(3)
        
        await browser.close()
    
    # Write results to CSV
    file_exists = Path(CSV_FILE).exists()
    
    with open(CSV_FILE, 'a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        
        # Write header if file is new
        if not file_exists:
            writer.writeheader()
        
        # Write all results
        for result in results:
            writer.writerow(result)
    
    # Print summary
    print("\n" + "="*60)
    print("SCRAPE SUMMARY")
    print("="*60)
    successful = sum(1 for r in results if r["status"] == "success")
    print(f"Total routes: {len(results)}")
    print(f"Successful: {successful}")
    print(f"Failed: {len(results) - successful}")
    print(f"Results appended to {CSV_FILE}")
    print("="*60)


if __name__ == "__main__":
    asyncio.run(main())
