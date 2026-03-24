#!/usr/bin/env python3
"""
Kyoto Trip — Food Place Validator & Discovery Tool

Validates every food place in index.html is real using:
  1. Google Maps Geocoding / Places API (place existence, coordinates, open hours, ratings)
  2. Yelp Fusion API (reviews, ratings, photos)
  3. OpenStreetMap Nominatim (free fallback for geocoding)

Also discovers new recommendations via Yelp search near each stop.

Usage:
  # Validate all places in index.html
  python3 validate_places.py --validate

  # Discover new food places near a location
  python3 validate_places.py --discover "Arashiyama, Kyoto"

  # Full run: validate + discover for all Day 3 stops
  python3 validate_places.py --full

  # Discover, validate & cache food for ALL itinerary stops
  python3 validate_places.py --discover-cached

  # Inject cached (un-injected) places into index.html
  python3 validate_places.py --inject

  # Show cache summary
  python3 validate_places.py --show-cache

  # Validate with Google Places API (needs key)
  GOOGLE_MAPS_API_KEY=xxx python3 validate_places.py --validate

  # Validate with Yelp API (needs key)
  YELP_API_KEY=xxx python3 validate_places.py --validate

Environment variables:
  GOOGLE_MAPS_API_KEY  — Google Maps Platform API key (Places API enabled)
  YELP_API_KEY         — Yelp Fusion API key (https://fusion.yelp.com/)

  Both are optional — the tool falls back to OpenStreetMap Nominatim (free, no key)
  for basic validation. For full stats (reviews, ratings, hours), set at least one key.
"""

import re
import json
import sys
import time
import argparse
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import quote, urlencode
from urllib.error import URLError, HTTPError
from html.parser import HTMLParser

# ─── CONFIG ───
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
GOOGLE_PLACES_URL = "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"
GOOGLE_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
YELP_SEARCH_URL = "https://api.yelp.com/v3/businesses/search"
YELP_MATCH_URL = "https://api.yelp.com/v3/businesses/matches"
INDEX_FILE = Path(__file__).parent / "index.html"
CACHE_FILE = Path(__file__).parent / "discovered_places.json"
WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"

import os
from datetime import datetime, timezone
GOOGLE_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
YELP_API_KEY = os.environ.get("YELP_API_KEY", "")


# ─── PLACE EXTRACTION FROM HTML ───

def extract_places_from_html(filepath=INDEX_FILE):
    """Parse index.html and extract all food/restaurant places with metadata."""
    html = filepath.read_text(encoding="utf-8")
    places = []

    # Extract card-name + card-jp pairs
    card_pattern = re.compile(
        r'<div class="card-name">(.+?)</div>\s*'
        r'(?:<div class="card-jp">(.+?)</div>)?',
        re.DOTALL
    )
    # Extract img-title + small pairs
    img_pattern = re.compile(
        r'<div class="img-title">(.+?)(?:<small>(.+?)</small>)?</div>',
        re.DOTALL
    )
    # Extract Maps coordinates from links (ll= param anywhere in URL)
    maps_pattern = re.compile(
        r'maps\.apple\.com/\?[^"]*?ll=([\d.]+),([\d.]+)'
    )
    # Extract Maps query
    maps_query_pattern = re.compile(
        r'maps\.apple\.com/\?q=([^&"]+)'
    )
    # Extract Yelp search queries
    yelp_pattern = re.compile(
        r'yelp\.com/search\?find_desc=([^&"]+)&find_loc=([^&"]+)'
    )

    # Find all food-related sections
    # Split by time-block to associate food with steps
    blocks = re.split(r'(<div class="time-block"[^>]*>.*?</div>)', html, flags=re.DOTALL)

    current_step = "General"
    for i, block in enumerate(blocks):
        step_match = re.search(r'STEP (\d+)', block)
        if step_match and 'time-block' in block:
            name_match = re.search(r'·\s*([^<]+?)(?:<span|$)', block)
            current_step = f"Step {step_match.group(1)}"
            if name_match:
                current_step += f" ({name_match.group(1).strip()})"
            continue

        # Find card-name entries (restaurants, cafes)
        for m in card_pattern.finditer(block):
            name = re.sub(r'<[^>]+>', '', m.group(1)).strip()
            jp = re.sub(r'<[^>]+>', '', m.group(2)).strip() if m.group(2) else ""

            # Skip non-food entries
            if any(skip in name.lower() for skip in [
                'what to wear', 'kyoto station —', 'dotonbori night',
                'tsutenkaku', 'momohada cosme'
            ]):
                continue

            # Find nearby coordinates — search around the name in full HTML
            lat, lng = None, None
            name_pos = html.find(m.group(1))
            if name_pos >= 0:
                context = html[name_pos:min(len(html), name_pos + 2000)]
                coords = maps_pattern.search(context)
            else:
                coords = None
            if coords:
                lat, lng = float(coords.group(1)), float(coords.group(2))

            # Find Yelp info
            yelp = yelp_pattern.search(context)
            yelp_query = yelp.group(1).replace('+', ' ') if yelp else ""
            yelp_loc = yelp.group(2).replace('+', ' ') if yelp else ""

            # Extract area from card-jp
            area = ""
            if jp:
                area_parts = [p.strip() for p in jp.split('·')]
                if len(area_parts) > 1:
                    area = area_parts[1]

            places.append({
                "name": name,
                "name_jp": jp.split('·')[0].strip() if jp else "",
                "area": area,
                "lat": lat,
                "lng": lng,
                "step": current_step,
                "yelp_query": yelp_query,
                "yelp_loc": yelp_loc or "Kyoto",
                "source": "card",
            })

        # Find img-title entries
        for m in img_pattern.finditer(block):
            name = re.sub(r'<[^>]+>', '', m.group(1)).strip()
            subtitle = re.sub(r'<[^>]+>', '', m.group(2)).strip() if m.group(2) else ""

            # Only food-related img-cards
            if any(skip in name.lower() for skip in [
                'ginkaku', 'kinkaku', 'nishiki market', 'kiyomizu',
                'gion district', 'arashiyama bamboo', 'toji temple',
                'byodo-in', 'dotonbori —', 'phoenix hall'
            ]):
                continue

            # Check for food indicators
            food_indicators = ['cafe', 'ramen', 'sushi', 'soba', 'matcha',
                             'gelato', 'food', 'restaurant', 'yoshoku',
                             'tsujiri', 'udon', 'shichimi', 'namagashi',
                             'warabi', 'okonomiyaki', 'fire', 'crepe']
            if not any(ind in name.lower() or ind in subtitle.lower()
                      for ind in food_indicators):
                continue

            context = html[max(0, m.start()-500):m.end()+1000]
            lat, lng = None, None
            coords = maps_pattern.search(context)
            if coords:
                lat, lng = float(coords.group(1)), float(coords.group(2))

            jp_name = ""
            if subtitle:
                jp_parts = subtitle.split('·')
                jp_name = jp_parts[0].strip()

            places.append({
                "name": name.split('—')[0].strip(),
                "name_jp": jp_name,
                "area": "",
                "lat": lat,
                "lng": lng,
                "step": current_step,
                "yelp_query": name.split('—')[0].strip(),
                "yelp_loc": "Kyoto",
                "source": "img-title",
            })

    # Deduplicate by name
    seen = set()
    unique = []
    for p in places:
        key = p["name"].lower().split('—')[0].strip()
        if key not in seen and len(key) > 3:
            seen.add(key)
            unique.append(p)

    return unique


# ─── VALIDATION BACKENDS ───

def _http_get(url, headers=None, timeout=10):
    """Simple HTTP GET returning parsed JSON."""
    req = Request(url, headers=headers or {})
    req.add_header("User-Agent", "KyotoTripValidator/1.0")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (URLError, HTTPError, json.JSONDecodeError) as e:
        return {"error": str(e)}


def validate_nominatim(name, name_jp="", lat=None, lng=None, area="Kyoto"):
    """Validate place exists via OpenStreetMap Nominatim (free, no API key)."""
    # Try Japanese name first (more precise), then English
    queries = []
    if name_jp:
        queries.append(f"{name_jp} {area}")
    queries.append(f"{name} {area}")
    if "Osaka" in area or "Dotonbori" in area or "Shinsekai" in area or "Namba" in area:
        queries.append(f"{name} Osaka")

    for q in queries:
        params = urlencode({
            "q": q,
            "format": "json",
            "limit": 3,
            "addressdetails": 1,
        })
        data = _http_get(f"{NOMINATIM_URL}?{params}")
        time.sleep(1.1)  # Nominatim rate limit: 1 req/sec

        if isinstance(data, list) and len(data) > 0:
            result = data[0]
            found_lat = float(result.get("lat", 0))
            found_lng = float(result.get("lon", 0))

            # Check if coordinates are in the right region (Japan)
            if 30 < found_lat < 40 and 130 < found_lng < 140:
                distance = None
                if lat and lng:
                    # Rough distance check (degrees)
                    dlat = abs(found_lat - lat)
                    dlng = abs(found_lng - lng)
                    distance = ((dlat**2 + dlng**2)**0.5) * 111000  # ~meters

                return {
                    "status": "FOUND",
                    "source": "OpenStreetMap",
                    "display_name": result.get("display_name", ""),
                    "lat": found_lat,
                    "lng": found_lng,
                    "distance_from_listed": f"{distance:.0f}m" if distance else "N/A",
                    "type": result.get("type", ""),
                    "query_used": q,
                }

    return {"status": "NOT_FOUND", "source": "OpenStreetMap", "queries_tried": queries}


def validate_google(name, name_jp="", lat=None, lng=None):
    """Validate via Google Places API (needs GOOGLE_MAPS_API_KEY)."""
    if not GOOGLE_API_KEY:
        return {"status": "SKIPPED", "reason": "No GOOGLE_MAPS_API_KEY set"}

    query = name_jp if name_jp else name
    params = urlencode({
        "input": f"{query} Kyoto",
        "inputtype": "textquery",
        "fields": "name,formatted_address,geometry,place_id,rating,user_ratings_total,business_status,opening_hours",
        "key": GOOGLE_API_KEY,
    })

    data = _http_get(f"{GOOGLE_PLACES_URL}?{params}")
    if "error" in data:
        return {"status": "ERROR", "source": "Google", "error": data["error"]}

    candidates = data.get("candidates", [])
    if not candidates:
        return {"status": "NOT_FOUND", "source": "Google"}

    c = candidates[0]
    result = {
        "status": "FOUND",
        "source": "Google Places",
        "name": c.get("name", ""),
        "address": c.get("formatted_address", ""),
        "rating": c.get("rating"),
        "total_reviews": c.get("user_ratings_total"),
        "business_status": c.get("business_status", "UNKNOWN"),
        "is_open": c.get("opening_hours", {}).get("open_now"),
    }

    geo = c.get("geometry", {}).get("location", {})
    if geo:
        result["lat"] = geo.get("lat")
        result["lng"] = geo.get("lng")

    # Get detailed info if we have a place_id
    place_id = c.get("place_id")
    if place_id:
        detail_params = urlencode({
            "place_id": place_id,
            "fields": "name,rating,user_ratings_total,opening_hours,business_status,price_level,url",
            "key": GOOGLE_API_KEY,
        })
        detail_data = _http_get(f"{GOOGLE_DETAILS_URL}?{detail_params}")
        detail = detail_data.get("result", {})
        if detail:
            result["hours"] = detail.get("opening_hours", {}).get("weekday_text", [])
            result["price_level"] = detail.get("price_level")
            result["google_url"] = detail.get("url")

    return result


def validate_yelp(name, location="Kyoto", lat=None, lng=None):
    """Validate via Yelp Fusion API (needs YELP_API_KEY)."""
    if not YELP_API_KEY:
        return {"status": "SKIPPED", "reason": "No YELP_API_KEY set"}

    headers = {"Authorization": f"Bearer {YELP_API_KEY}"}

    params = {
        "term": name,
        "location": f"{location}, Japan",
        "limit": 3,
        "categories": "restaurants,food,cafes",
    }
    if lat and lng:
        params["latitude"] = lat
        params["longitude"] = lng

    url = f"{YELP_SEARCH_URL}?{urlencode(params)}"
    data = _http_get(url, headers=headers)

    if "error" in data:
        return {"status": "ERROR", "source": "Yelp", "error": data["error"]}

    businesses = data.get("businesses", [])
    if not businesses:
        return {"status": "NOT_FOUND", "source": "Yelp"}

    b = businesses[0]
    return {
        "status": "FOUND",
        "source": "Yelp",
        "name": b.get("name", ""),
        "rating": b.get("rating"),
        "review_count": b.get("review_count"),
        "price": b.get("price", ""),
        "is_closed": b.get("is_closed", None),
        "categories": ", ".join(c["title"] for c in b.get("categories", [])),
        "address": ", ".join(b.get("location", {}).get("display_address", [])),
        "phone": b.get("phone", ""),
        "yelp_url": b.get("url", ""),
        "lat": b.get("coordinates", {}).get("latitude"),
        "lng": b.get("coordinates", {}).get("longitude"),
    }


# ─── DISCOVERY ───

def discover_places(location, lat=None, lng=None, categories="restaurants,food"):
    """Discover new food places near a location using available APIs."""
    results = []

    # Yelp discovery
    if YELP_API_KEY:
        headers = {"Authorization": f"Bearer {YELP_API_KEY}"}
        params = {
            "location": f"{location}, Japan",
            "limit": 10,
            "sort_by": "rating",
            "categories": categories,
        }
        if lat and lng:
            params["latitude"] = lat
            params["longitude"] = lng
            params["radius"] = 1000  # 1km

        url = f"{YELP_SEARCH_URL}?{urlencode(params)}"
        data = _http_get(url, headers=headers)
        for b in data.get("businesses", []):
            results.append({
                "name": b.get("name", ""),
                "rating": b.get("rating"),
                "review_count": b.get("review_count"),
                "price": b.get("price", ""),
                "categories": ", ".join(c["title"] for c in b.get("categories", [])),
                "address": ", ".join(b.get("location", {}).get("display_address", [])),
                "yelp_url": b.get("url", ""),
                "source": "Yelp",
            })

    # Nominatim discovery (find restaurants/cafes nearby)
    if lat and lng:
        # Search for amenity=restaurant near coordinates
        overpass_url = "https://overpass-api.de/api/interpreter"
        query = f"""
        [out:json][timeout:10];
        (
          node["amenity"="restaurant"](around:500,{lat},{lng});
          node["amenity"="cafe"](around:500,{lat},{lng});
        );
        out body 5;
        """
        try:
            import urllib.request
            req = urllib.request.Request(overpass_url, data=f"data={quote(query)}".encode())
            req.add_header("User-Agent", "KyotoTripValidator/1.0")
            with urllib.request.urlopen(req, timeout=15) as resp:
                osm_data = json.loads(resp.read().decode())
                for el in osm_data.get("elements", []):
                    tags = el.get("tags", {})
                    name = tags.get("name:en") or tags.get("name", "")
                    if name:
                        results.append({
                            "name": name,
                            "name_jp": tags.get("name:ja", ""),
                            "cuisine": tags.get("cuisine", ""),
                            "lat": el.get("lat"),
                            "lng": el.get("lon"),
                            "source": "OpenStreetMap",
                        })
        except Exception:
            pass

    return results


# ─── MAIN VALIDATION PIPELINE ───

def validate_place(place):
    """Run all available validators on a single place."""
    name = place["name"]
    name_jp = place.get("name_jp", "")
    lat = place.get("lat")
    lng = place.get("lng")
    area = place.get("area", "Kyoto")

    print(f"\n{'='*60}")
    print(f"  {name}")
    if name_jp:
        print(f"  {name_jp}")
    print(f"  Step: {place.get('step', '?')} | Listed coords: {lat}, {lng}")
    print(f"{'='*60}")

    results = {}
    overall_status = "UNVERIFIED"

    # 1. OpenStreetMap (always available, free)
    print("  [OSM] Checking OpenStreetMap...", end=" ", flush=True)
    osm = validate_nominatim(name, name_jp, lat, lng, area)
    results["osm"] = osm
    if osm["status"] == "FOUND":
        print(f"✅ Found — {osm.get('display_name', '')[:60]}")
        overall_status = "VERIFIED"
    else:
        print(f"⚠️  Not found")

    # 2. Google Places (if key available)
    if GOOGLE_API_KEY:
        print("  [Google] Checking Google Places...", end=" ", flush=True)
        google = validate_google(name, name_jp, lat, lng)
        results["google"] = google
        if google["status"] == "FOUND":
            rating = google.get("rating", "?")
            reviews = google.get("total_reviews", "?")
            status = google.get("business_status", "?")
            print(f"✅ {rating}★ ({reviews} reviews) — {status}")
            overall_status = "VERIFIED"
            if status == "CLOSED_PERMANENTLY":
                overall_status = "CLOSED"
                print(f"  ❌ PERMANENTLY CLOSED!")
        else:
            print(f"⚠️  {google['status']}")

    # 3. Yelp (if key available)
    if YELP_API_KEY:
        yelp_query = place.get("yelp_query") or name
        yelp_loc = place.get("yelp_loc", "Kyoto")
        print(f"  [Yelp] Checking Yelp...", end=" ", flush=True)
        yelp = validate_yelp(yelp_query, yelp_loc, lat, lng)
        results["yelp"] = yelp
        if yelp["status"] == "FOUND":
            rating = yelp.get("rating", "?")
            reviews = yelp.get("review_count", "?")
            closed = yelp.get("is_closed", False)
            print(f"✅ {rating}★ ({reviews} reviews) {'— CLOSED!' if closed else ''}")
            if not closed:
                overall_status = "VERIFIED"
            else:
                overall_status = "CLOSED"
        else:
            print(f"⚠️  {yelp['status']}")

    # Summary
    emoji = {"VERIFIED": "✅", "CLOSED": "❌", "UNVERIFIED": "⚠️"}
    print(f"\n  → Overall: {emoji.get(overall_status, '?')} {overall_status}")

    return {
        "place": place,
        "overall_status": overall_status,
        "results": results,
    }


def run_validation():
    """Validate all places from index.html."""
    places = extract_places_from_html()
    print(f"\n🔍 Found {len(places)} food places in index.html\n")
    print(f"APIs available:")
    print(f"  OpenStreetMap Nominatim: ✅ (free, always on)")
    print(f"  Google Places API:       {'✅' if GOOGLE_API_KEY else '❌ (set GOOGLE_MAPS_API_KEY)'}")
    print(f"  Yelp Fusion API:         {'✅' if YELP_API_KEY else '❌ (set YELP_API_KEY)'}")

    all_results = []
    verified = 0
    not_found = 0
    closed = 0

    for place in places:
        result = validate_place(place)
        all_results.append(result)
        s = result["overall_status"]
        if s == "VERIFIED":
            verified += 1
        elif s == "CLOSED":
            closed += 1
        else:
            not_found += 1

    # Summary
    print(f"\n{'='*60}")
    print(f"  VALIDATION SUMMARY")
    print(f"{'='*60}")
    print(f"  Total places:  {len(places)}")
    print(f"  ✅ Verified:    {verified}")
    print(f"  ⚠️  Unverified: {not_found}")
    print(f"  ❌ Closed:      {closed}")

    if not_found > 0:
        print(f"\n  ⚠️  Unverified places (may need manual check):")
        for r in all_results:
            if r["overall_status"] == "UNVERIFIED":
                print(f"     - {r['place']['name']} ({r['place']['step']})")

    if closed > 0:
        print(f"\n  ❌ Closed places (REMOVE from site!):")
        for r in all_results:
            if r["overall_status"] == "CLOSED":
                print(f"     - {r['place']['name']} ({r['place']['step']})")

    # Save detailed results
    output = Path(__file__).parent / "validation_results.json"
    with open(output, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\n  Detailed results saved to: {output}")

    return all_results


def run_discovery(location=None, lat=None, lng=None):
    """Discover new food places near trip stops."""
    stops = [
        {"name": "Toji Temple, Kyoto", "lat": 34.9804, "lng": 135.7477},
        {"name": "Arashiyama, Kyoto", "lat": 35.0173, "lng": 135.6717},
        {"name": "Uji, Kyoto", "lat": 34.8895, "lng": 135.8078},
        {"name": "Kyoto Station", "lat": 34.9858, "lng": 135.7588},
        {"name": "Dotonbori, Osaka", "lat": 34.6687, "lng": 135.5013},
        {"name": "Shinsekai, Osaka", "lat": 34.6527, "lng": 135.5063},
    ]

    if location:
        stops = [{"name": location, "lat": lat, "lng": lng}]

    for stop in stops:
        print(f"\n{'='*60}")
        print(f"  🔎 Discovering near: {stop['name']}")
        print(f"{'='*60}")

        places = discover_places(
            stop["name"],
            lat=stop.get("lat"),
            lng=stop.get("lng"),
        )

        if not places:
            print("  No places found (set YELP_API_KEY for better results)")
            continue

        for i, p in enumerate(places[:5], 1):
            print(f"\n  {i}. {p['name']}")
            if p.get("rating"):
                print(f"     ⭐ {p['rating']} ({p.get('review_count', '?')} reviews)")
            if p.get("price"):
                print(f"     💴 {p['price']}")
            if p.get("categories"):
                print(f"     🏷️  {p['categories']}")
            if p.get("address"):
                print(f"     📍 {p['address']}")
            if p.get("yelp_url"):
                print(f"     🔗 {p['yelp_url']}")


def run_full():
    """Run validation + discovery."""
    print("=" * 60)
    print("  PHASE 1: VALIDATING ALL EXISTING PLACES")
    print("=" * 60)
    run_validation()

    print("\n\n")
    print("=" * 60)
    print("  PHASE 2: DISCOVERING NEW RECOMMENDATIONS")
    print("=" * 60)
    run_discovery()


# ─── STEP REFERENCE TABLE ───

DISCOVERY_STEPS = [
    {"step_id": "act-philosophers-path", "location": "Ginkaku-ji, Kyoto", "lat": 35.0270, "lng": 135.7982, "area": "Ginkaku-ji"},
    {"step_id": "act-kinkakuji", "location": "Kinkaku-ji, Kyoto", "lat": 35.0394, "lng": 135.7292, "area": "Kinkaku-ji"},
    {"step_id": "act-nishiki", "location": "Nishiki Market, Kyoto", "lat": 35.0050, "lng": 135.7650, "area": "Nishiki"},
    {"step_id": "act-lunch", "location": "Nishiki/Shijo, Kyoto", "lat": 35.0055, "lng": 135.7645, "area": "Shijo"},
    {"step_id": "act-kiyomizu", "location": "Kiyomizu, Kyoto", "lat": 34.9949, "lng": 135.7850, "area": "Kiyomizu"},
    {"step_id": "act-gion", "location": "Gion, Kyoto", "lat": 35.0036, "lng": 135.7756, "area": "Gion"},
    {"step_id": "act-dinner", "location": "Pontocho, Kyoto", "lat": 35.0050, "lng": 135.7700, "area": "Pontocho"},
    {"step_id": "act-shinpuku", "location": "Kyoto Station", "lat": 34.9875, "lng": 135.7590, "area": "Kyoto Station"},
    {"step_id": "act-d3-toji", "location": "Toji Temple, Kyoto", "lat": 34.9804, "lng": 135.7477, "area": "Toji"},
    {"step_id": "act-d3-arashiyama", "location": "Arashiyama, Kyoto", "lat": 35.0173, "lng": 135.6717, "area": "Arashiyama"},
    {"step_id": "act-d3-uji", "location": "Uji, Kyoto", "lat": 34.8895, "lng": 135.8078, "area": "Uji"},
    {"step_id": "act-d3-souvenirs", "location": "Kyoto Station", "lat": 34.9858, "lng": 135.7588, "area": "Kyoto Station"},
    {"step_id": "act-d3-lunch", "location": "Kyoto Station", "lat": 34.9858, "lng": 135.7588, "area": "Kyoto Station"},
    {"step_id": "act-d3-dotonbori", "location": "Dotonbori, Osaka", "lat": 34.6687, "lng": 135.5013, "area": "Dotonbori"},
    {"step_id": "act-d3-shinsekai", "location": "Shinsekai, Osaka", "lat": 34.6527, "lng": 135.5063, "area": "Shinsekai"},
    {"step_id": "act-d3-night", "location": "Namba, Osaka", "lat": 34.6687, "lng": 135.5013, "area": "Namba"},
]

# Steps to skip (transit/shopping, not food-relevant)
SKIP_STEPS = {"act-d3-momohada", "act-d3-train-uji", "act-d3-train-back", "act-d3-osaka-train"}

# Same-area dedup: assign to the food-specific step, not duplicated
AREA_PRIMARY_STEP = {
    "Kyoto Station": "act-d3-lunch",
    "Dotonbori": "act-d3-dotonbori",
    "Namba": "act-d3-night",
}


# ─── SLUGIFY ───

def slugify(text):
    """Create a URL-friendly slug from text."""
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_]+', '-', text)
    text = re.sub(r'-+', '-', text)
    return text.strip('-')


# ─── WIKIMEDIA IMAGE SEARCH ───

def search_wikimedia_image(query):
    """Search Wikimedia Commons for a food image by dish name.
    Returns {"url": "...", "alt": "..."} or None.
    """
    # Try progressively broader queries
    queries = [query]
    # Add broader fallback: just the dish type
    words = query.split()
    if len(words) > 2:
        queries.append(" ".join(words[:2]))
    if len(words) > 1:
        queries.append(words[0])  # single keyword like "takoyaki"

    for q in queries:
        params = urlencode({
            "action": "query",
            "generator": "search",
            "gsrnamespace": 6,
            "gsrsearch": q,
            "gsrlimit": 5,
            "prop": "imageinfo",
            "iiprop": "url|extmetadata",
            "iiurlwidth": 800,
            "format": "json",
        })
        data = _http_get(f"{WIKIMEDIA_API}?{params}")
        time.sleep(0.5)  # Be polite to Wikimedia

        if "error" in data or "query" not in data:
            continue

        pages = data.get("query", {}).get("pages", {})
        for page_id, page in sorted(pages.items()):
            imageinfo = page.get("imageinfo", [])
            if not imageinfo:
                continue
            info = imageinfo[0]
            thumb_url = info.get("thumburl") or info.get("url")
            if not thumb_url:
                continue
            # Skip SVGs, icons, logos
            if any(ext in thumb_url.lower() for ext in ['.svg', 'icon', 'logo', 'flag']):
                continue
            # Get description from metadata
            meta = info.get("extmetadata", {})
            alt = meta.get("ObjectName", {}).get("value", q)
            return {"url": thumb_url, "alt": alt}

    return None


# ─── CACHE MANAGEMENT ───

def load_cache(filepath=CACHE_FILE):
    """Load discovered_places.json cache. Returns cache dict."""
    if not filepath.exists():
        return {
            "version": 1,
            "last_updated": None,
            "places": {},
        }
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "places" not in data:
            raise ValueError("Invalid cache format")
        return data
    except (json.JSONDecodeError, ValueError):
        # Backup corrupted file
        bak = filepath.with_suffix(".json.bak")
        print(f"  ⚠️  Cache corrupted, backing up to {bak}")
        import shutil
        shutil.copy2(filepath, bak)
        return {"version": 1, "last_updated": None, "places": {}}


def save_cache(cache, filepath=CACHE_FILE):
    """Save cache to discovered_places.json. Preserves all existing entries."""
    cache["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)
    print(f"  💾 Cache saved: {len(cache['places'])} places in {filepath.name}")


# ─── FUZZY NAME MATCHING ───

def _normalize_name(name):
    """Normalize a place name for fuzzy matching."""
    name = name.lower().strip()
    name = re.sub(r'[^\w\s]', '', name)
    name = re.sub(r'\s+', ' ', name)
    # Remove common suffixes
    for suffix in ['kyoto', 'osaka', 'japan', 'restaurant', 'cafe']:
        name = name.replace(suffix, '')
    return name.strip()


def _names_match(name1, name2):
    """Check if two place names are similar enough to be the same place."""
    n1 = _normalize_name(name1)
    n2 = _normalize_name(name2)
    if n1 == n2:
        return True
    # One contains the other
    if n1 in n2 or n2 in n1:
        return True
    # Check word overlap
    words1 = set(n1.split())
    words2 = set(n2.split())
    if len(words1) > 0 and len(words2) > 0:
        overlap = words1 & words2
        shorter = min(len(words1), len(words2))
        if shorter > 0 and len(overlap) / shorter >= 0.6:
            return True
    return False


def _get_existing_html_names():
    """Extract all food place names (EN + JP) already in index.html."""
    places = extract_places_from_html()
    names = []
    for p in places:
        names.append(p["name"])
        if p.get("name_jp"):
            names.append(p["name_jp"])
    return names

# Global chains / uninteresting places to skip
SKIP_NAMES = {
    "starbucks", "doutor", "mcdonald", "mcdonalds", "subway",
    "tully", "tullys", "komeda", "veloce", "cafe veloce",
    "mos burger", "lotteria", "first kitchen", "yoshinoya",
    "matsuya", "sukiya", "coco ichibanya", "gusto", "saizeriya",
    "jonathan", "denny", "dennys",
}


# ─── DISCOVER AND CACHE ───

def _pick_emoji(categories):
    """Pick an emoji based on food categories."""
    cats = (categories or "").lower()
    if "ramen" in cats or "noodle" in cats:
        return "🍜"
    if "sushi" in cats:
        return "🍣"
    if "cafe" in cats or "coffee" in cats:
        return "☕"
    if "bakery" in cats or "bread" in cats:
        return "🥐"
    if "curry" in cats:
        return "🍛"
    if "udon" in cats or "soba" in cats:
        return "🍜"
    if "tempura" in cats:
        return "🍤"
    if "takoyaki" in cats or "okonomiyaki" in cats:
        return "🐙"
    if "dessert" in cats or "sweet" in cats or "ice cream" in cats:
        return "🍦"
    if "yakitori" in cats:
        return "🍢"
    if "izakaya" in cats or "bar" in cats:
        return "🍶"
    return "🍴"


def discover_and_cache(step_id, location, lat, lng, area, cache):
    """Discover new places for a step, validate, fetch images, update cache.
    Returns list of newly added place slugs.
    """
    existing_html_names = _get_existing_html_names()
    new_slugs = []

    # Discover candidates
    candidates = discover_places(location, lat=lat, lng=lng)
    if not candidates:
        return new_slugs

    for c in candidates:
        name = c.get("name", "").strip()
        if not name or len(name) < 2:
            continue

        # Skip global chains
        if _normalize_name(name) in SKIP_NAMES or any(
            chain in name.lower() for chain in SKIP_NAMES
        ):
            continue

        slug = slugify(f"{name}-{area}")

        # Skip if already in cache (exact slug)
        if slug in cache["places"]:
            continue

        # Skip if same name already cached for any area (cross-area dedup)
        if any(_names_match(name, p["name"]) for p in cache["places"].values()):
            continue

        # Skip if already in index.html (fuzzy match against EN + JP names)
        if any(_names_match(name, html_name) for html_name in existing_html_names):
            continue

        # Validate
        print(f"    Validating: {name}...", end=" ", flush=True)
        osm_result = validate_nominatim(name, c.get("name_jp", ""), lat, lng, area)
        yelp_result = {"status": "SKIPPED"}
        if YELP_API_KEY:
            yelp_result = validate_yelp(name, location, lat, lng)

        sources = []
        if osm_result.get("status") == "FOUND":
            sources.append("OpenStreetMap")
        if yelp_result.get("status") == "FOUND":
            sources.append("Yelp")

        if not sources:
            print("❌ unverified, skipping")
            continue

        print(f"✅ verified ({', '.join(sources)})")

        # Get Yelp details for price, rating, URL
        yelp_url = yelp_result.get("yelp_url", "")
        price = c.get("price") or yelp_result.get("price", "")
        rating = c.get("rating") or yelp_result.get("rating")
        categories = c.get("categories") or yelp_result.get("categories", "")

        # Build maps URL
        place_lat = c.get("lat") or osm_result.get("lat") or lat
        place_lng = c.get("lng") or osm_result.get("lng") or lng
        maps_url = f"https://maps.apple.com/?q={quote(name)}&ll={place_lat},{place_lng}"

        # Search for a Wikimedia image by cuisine/dish type
        image_data = None
        search_terms = []
        if categories:
            # Use the first category as search term
            first_cat = categories.split(",")[0].strip()
            search_terms.append(f"{first_cat} Japanese food")
        search_terms.append(f"{name} food")
        if "Osaka" in location or "Dotonbori" in location or "Shinsekai" in location:
            search_terms.append(f"Osaka street food")
        else:
            search_terms.append(f"Kyoto food")

        for term in search_terms:
            image_data = search_wikimedia_image(term)
            if image_data:
                break

        # Build cache entry
        entry = {
            "slug": slug,
            "name": name,
            "name_jp": c.get("name_jp", ""),
            "description": f"{categories}" if categories else f"Local restaurant near {area}",
            "top_dishes": [],
            "price_range": price,
            "maps_url": maps_url,
            "yelp_url": yelp_url,
            "image_url": image_data["url"] if image_data else None,
            "image_alt": image_data["alt"] if image_data else None,
            "lat": place_lat,
            "lng": place_lng,
            "associated_step_id": step_id,
            "area": area,
            "emoji": _pick_emoji(categories),
            "rating": rating,
            "validation": {
                "status": "VERIFIED",
                "sources": sources,
                "validated_at": datetime.now(timezone.utc).isoformat(),
            },
            "added_at": datetime.now(timezone.utc).isoformat(),
            "injected_into_html": False,
        }

        cache["places"][slug] = entry
        new_slugs.append(slug)

    return new_slugs


# ─── CACHED DISCOVERY (ALL STOPS) ───

def run_discover_cached():
    """Run discovery for all stops, updating the persistent cache."""
    cache = load_cache()
    total_new = 0
    summary = {}

    print(f"\n{'='*60}")
    print(f"  FOOD DISCOVERY — All Itinerary Stops")
    print(f"{'='*60}")
    print(f"  Cache: {len(cache['places'])} existing places")
    print(f"  APIs: OSM ✅ | Yelp {'✅' if YELP_API_KEY else '❌'}")
    print()

    # Track areas already processed (dedup same-area stops)
    processed_areas = {}

    for step in DISCOVERY_STEPS:
        step_id = step["step_id"]
        location = step["location"]
        area = step["area"]

        # Dedup: if this area was already processed for a primary step, skip
        primary = AREA_PRIMARY_STEP.get(area)
        if primary and primary != step_id:
            # This step shares an area with a primary step — skip discovery
            # but don't skip if the primary hasn't been processed yet
            if area in processed_areas:
                print(f"  ⏭  {step_id} — dedup with {processed_areas[area]}")
                continue

        print(f"\n  🔎 {step_id} — {location}")
        new_slugs = discover_and_cache(
            step_id, location, step["lat"], step["lng"], area, cache
        )
        processed_areas[area] = step_id
        summary[step_id] = new_slugs
        total_new += len(new_slugs)

        if new_slugs:
            for slug in new_slugs:
                p = cache["places"][slug]
                img = "🖼" if p.get("image_url") else "  "
                print(f"    + {p['name']} {img} {p.get('price_range', '')}")

    save_cache(cache)

    # Print summary
    print(f"\n{'='*60}")
    print(f"  DISCOVERY SUMMARY")
    print(f"{'='*60}")
    print(f"  New places found:  {total_new}")
    print(f"  Total in cache:    {len(cache['places'])}")
    print()

    if total_new > 0:
        print(f"  {'Step':<28} {'New':>4}  Places")
        print(f"  {'─'*28} {'─'*4}  {'─'*30}")
        for step_id, slugs in summary.items():
            if slugs:
                names = ", ".join(cache["places"][s]["name"] for s in slugs[:3])
                if len(slugs) > 3:
                    names += f" +{len(slugs)-3} more"
                print(f"  {step_id:<28} {len(slugs):>4}  {names}")
    else:
        print("  No new places found. All candidates are already cached or in HTML.")

    return cache


# ─── HTML INJECTION ───

def _build_img_card_html(place):
    """Build an img-card HTML string for a place with an image."""
    p = place
    dishes_html = ""
    if p.get("top_dishes"):
        dish_items = "\n".join(
            f'<div class="dish"><strong>{d.get("name", "")}</strong> — {d.get("desc", "")}</div>'
            for d in p["top_dishes"][:3]
        )
        dishes_html = f"""<div class="dishes">
<div class="dishes-title">{p.get('emoji', '🍴')} Top picks</div>
{dish_items}
</div>"""

    yelp_link = ""
    if p.get("yelp_url"):
        yelp_link = f'\n<a class="maps-link" href="{p["yelp_url"]}" target="_blank">⭐ Yelp</a>'

    maps_link = ""
    if p.get("maps_url"):
        maps_link = f'\n<a class="maps-link" href="{p["maps_url"]}" target="_blank">📍 Maps</a>'

    price_pill = ""
    if p.get("price_range"):
        price_pill = f'<span class="meta-pill">{p["price_range"]}</span>'

    return f"""<div class="img-card">
<div class="img-banner" style="height:140px;background-image:url('{p["image_url"]}')">
<div class="img-title">{p["name"]}<small>{p.get("name_jp", "")} · {p.get("area", "")} · Walk-in</small></div>
</div>
<div class="img-body">
<div class="desc">{p.get("description", "")}</div>
{dishes_html}<div class="card-meta">{price_pill}<span class="meta-pill">Walk-in</span>{yelp_link}{maps_link}</div>
</div>
</div>"""


def _build_card_html(place):
    """Build a card HTML string for a place without an image."""
    p = place

    yelp_link = ""
    if p.get("yelp_url"):
        yelp_link = f'\n<a class="maps-link" href="{p["yelp_url"]}" target="_blank">⭐ Yelp</a>'

    maps_link = ""
    if p.get("maps_url"):
        maps_link = f'\n<a class="maps-link" href="{p["maps_url"]}" target="_blank">📍 Maps</a>'

    price_pill = ""
    if p.get("price_range"):
        price_pill = f'<span class="meta-pill">{p["price_range"]}</span>'

    return f"""<div class="card">
<div class="card-top">
<div class="card-emoji">{p.get("emoji", "🍴")}</div>
<div class="card-info">
<div class="card-name">{p["name"]}</div>
<div class="card-jp">{p.get("name_jp", "")} · {p.get("area", "")}</div>
<div class="card-desc">{p.get("description", "")}</div>
<div class="card-meta">{price_pill}<span class="meta-pill">Walk-in</span>{yelp_link}{maps_link}</div>
</div>
</div>
</div>"""


def run_inject():
    """Inject un-injected cached places into index.html."""
    cache = load_cache()
    html = INDEX_FILE.read_text(encoding="utf-8")

    # Collect un-injected places grouped by step_id
    by_step = {}
    for slug, place in cache["places"].items():
        if not place.get("injected_into_html", False):
            step_id = place.get("associated_step_id", "")
            by_step.setdefault(step_id, []).append((slug, place))

    if not by_step:
        print("  No un-injected places in cache. Nothing to do.")
        return

    injected_count = 0

    for step_id, places in by_step.items():
        # Find the time-block with this id
        block_pattern = f'id="{step_id}"'
        block_pos = html.find(block_pattern)
        if block_pos < 0:
            print(f"  ⚠️  Step {step_id} not found in HTML, skipping {len(places)} places")
            continue

        # Build cards HTML
        cards_html = ""
        for slug, place in places:
            if place.get("image_url"):
                cards_html += "\n" + _build_img_card_html(place)
            else:
                cards_html += "\n" + _build_card_html(place)

        # Look for existing food-nearby after this step's content
        # Search forward from the time-block for the next time-block or section
        search_start = block_pos
        # Find the next time-block after this one
        next_block = html.find('<div class="time-block"', search_start + len(block_pattern))
        # Also check for section boundaries
        next_section = html.find('<div class="section"', search_start + len(block_pattern))
        # Use the nearest boundary
        boundary = len(html)
        if next_block > 0:
            boundary = min(boundary, next_block)
        if next_section > 0:
            boundary = min(boundary, next_section)

        region = html[search_start:boundary]

        # Check if there's already a food-nearby details in this region
        food_nearby_offset = region.rfind('<details class="food-nearby">')
        if food_nearby_offset >= 0:
            # Find the inner div and append cards before its closing tag
            inner_pos = search_start + food_nearby_offset
            # Find the closing </div> for food-nearby-inner
            close_inner = html.find('</div>\n</details>', inner_pos)
            if close_inner > 0:
                insert_pos = close_inner
                html = html[:insert_pos] + cards_html + "\n" + html[insert_pos:]
            else:
                # Fallback: insert before </details>
                close_details = html.find('</details>', inner_pos)
                if close_details > 0:
                    html = html[:close_details] + cards_html + "\n" + html[close_details:]
        else:
            # Create a new food-nearby block
            # Insert just before the next time-block/section boundary
            emoji = places[0][1].get("emoji", "🍴")
            area = places[0][1].get("area", "nearby")
            summary_text = f"{emoji} More food near {area} ({len(places)} discovered)"
            new_block = f"""\n<details class="food-nearby">
<summary>{summary_text}</summary>
<div class="food-nearby-inner">
{cards_html}
</div>
</details>\n"""
            html = html[:boundary] + new_block + html[boundary:]

        # Mark as injected
        for slug, place in places:
            cache["places"][slug]["injected_into_html"] = True
            injected_count += 1
            print(f"  ✅ Injected: {place['name']} → {step_id}")

    # Save updated HTML and cache
    INDEX_FILE.write_text(html, encoding="utf-8")
    save_cache(cache)

    print(f"\n  📝 Injected {injected_count} new food cards into index.html")


# ─── SHOW CACHE ───

def run_show_cache():
    """Print cache summary."""
    cache = load_cache()
    places = cache.get("places", {})

    if not places:
        print("  Cache is empty. Run --discover-cached first.")
        return

    print(f"\n{'='*60}")
    print(f"  CACHE SUMMARY — {len(places)} places")
    print(f"  Last updated: {cache.get('last_updated', 'never')}")
    print(f"{'='*60}")

    # Group by step
    by_step = {}
    for slug, p in places.items():
        step = p.get("associated_step_id", "unknown")
        by_step.setdefault(step, []).append(p)

    for step_id, step_places in sorted(by_step.items()):
        injected = sum(1 for p in step_places if p.get("injected_into_html"))
        with_image = sum(1 for p in step_places if p.get("image_url"))
        print(f"\n  {step_id} ({len(step_places)} places, {injected} injected, {with_image} with images)")
        for p in step_places:
            img = "🖼" if p.get("image_url") else "  "
            inj = "✅" if p.get("injected_into_html") else "⬜"
            rating = f"⭐{p['rating']}" if p.get("rating") else ""
            print(f"    {inj} {img} {p['name']} {p.get('price_range', '')} {rating}")


# ─── CLI ───

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Validate and discover food places for Kyoto trip"
    )
    parser.add_argument("--validate", action="store_true",
                       help="Validate all places in index.html")
    parser.add_argument("--discover", type=str, metavar="LOCATION",
                       help="Discover food near a location (e.g. 'Arashiyama, Kyoto')")
    parser.add_argument("--full", action="store_true",
                       help="Run full validation + discovery")
    parser.add_argument("--extract", action="store_true",
                       help="Just extract and list all places from HTML")
    parser.add_argument("--discover-cached", action="store_true",
                       help="Discover food for all stops, validate, cache results")
    parser.add_argument("--inject", action="store_true",
                       help="Inject un-injected cached places into index.html")
    parser.add_argument("--show-cache", action="store_true",
                       help="Show cache summary")

    args = parser.parse_args()

    if args.extract:
        places = extract_places_from_html()
        print(f"Found {len(places)} food places:\n")
        for i, p in enumerate(places, 1):
            coords = f"({p['lat']}, {p['lng']})" if p['lat'] else "(no coords)"
            print(f"  {i:2d}. {p['name']}")
            if p['name_jp']:
                print(f"      JP: {p['name_jp']}")
            print(f"      {p['step']} | {coords}")
        sys.exit(0)

    if args.show_cache:
        run_show_cache()
    elif args.discover_cached:
        run_discover_cached()
    elif args.inject:
        run_inject()
    elif args.validate:
        run_validation()
    elif args.discover:
        run_discovery(location=args.discover)
    elif args.full:
        run_full()
    else:
        # Default: extract + validate
        run_validation()
