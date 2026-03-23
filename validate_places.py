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

import os
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

    if args.validate:
        run_validation()
    elif args.discover:
        run_discovery(location=args.discover)
    elif args.full:
        run_full()
    else:
        # Default: extract + validate
        run_validation()
