---
description: Discover, validate, and inject new food places for each itinerary stop
allowed-tools: [Read, Write, Edit, Bash, Grep, Glob]
---

# Discover, Validate & Inject Food Places

You are helping Alex discover new food places near each itinerary stop in his Kyoto/Osaka trip dashboard.

## Phase 1: Discover & Validate

1. Read `discovered_places.json` cache (if it exists) to see what's already been found
2. Run the discovery command:

```bash
python3 validate_places.py --discover-cached
```

This will:
- Iterate all 16 food-relevant stops (skipping transit: `act-d3-momohada`, `act-d3-train-uji`, `act-d3-train-back`, `act-d3-osaka-train`)
- Discover places near each stop via Yelp/Overpass
- Validate each candidate (OSM + Yelp)
- Fetch Wikimedia Commons images by dish name
- Append new places to `discovered_places.json` (existing slugs are preserved)

3. Show the user a summary table: new places grouped by step, with name, rating, price, and image status (✅ has image / ❌ no image)

## Phase 2: Inject into HTML (after user confirms)

**Wait for the user to confirm before proceeding to injection.**

4. Run the injection command:

```bash
python3 validate_places.py --inject
```

This will:
- Read the cache for places where `injected_into_html` is `false`
- For each place, find the matching `time-block` by `associated_step_id` in `index.html`
- If a `<details class="food-nearby">` already exists after that step, append new cards inside it
- If not, create a new `<details class="food-nearby">` block after the step's content
- Use `img-card` pattern (with banner image) when a Wikimedia image exists, `card` pattern (with emoji) when not
- Mark `injected_into_html: true` in the cache

5. Report what was injected and where

## Important Notes

- **Append-only**: Never remove existing places from the cache or HTML
- **Dedup**: Same-area stops share discovered places — assign to the food-specific step, don't duplicate
- **Validation required**: Every place must be confirmed by at least 1 source (OSM, Yelp, or Google) before caching
- **Images**: Search Wikimedia Commons by dish name (e.g. "takoyaki osaka"), not restaurant name. Fall back to broader cuisine terms if specific dish fails
- If the `--discover-cached` step finds 0 new places, tell the user — no injection needed
- You can also check cache status with: `python3 validate_places.py --show-cache`
