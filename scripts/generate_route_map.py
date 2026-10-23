#!/usr/bin/env python3
"""
generate_route_map.py — SHELL FOS road-route map generator

For one user (or all users) on one date, this script:
  1. Reads the LocationLogs sheet (via a Google service account).
  2. Filters to that user/date, drops "Location unknown" (0,0) rows,
     and sorts the remaining points by Time -> the visit sequence.
  3. Calls the free OSRM public routing API with ALL waypoints in one
     request so the returned line follows actual roads, in the correct
     order, and gives a real driving distance in km.
  4. Writes a self-contained interactive map page (Leaflet + OpenStreetMap):
     road-following route line + numbered pin for every sequence point
     (green=start, red=end, blue=in-between). Pinch / double-tap / scroll
     to zoom, drag to pan, tap a pin for its time and address.
  5. Saves the page to docs/routes/<UserID>/<date>.html (served by GitHub
     Pages once the repo has Pages enabled on the /docs folder). The
     Leaflet library is served from docs/vendor/leaflet (no CDN needed).
  6. Upserts a row into the RouteMaps sheet tab so the backend app can
     look up the generated map link + distance for that user/date.

Usage:
    python generate_route_map.py --date 2026-09-09 --user-id D91689
    python generate_route_map.py --date 2026-09-09              # all users that day

Required environment variables:
    SHEET_ID                        Google Sheet ID (LocationLogs / RouteMaps live here)
    GOOGLE_APPLICATION_CREDENTIALS  Path to a service-account JSON key file
    PAGES_BASE_URL                  e.g. https://<owner>.github.io/<repo>
                                     (used to build the MapURL written to RouteMaps)
"""

import argparse
import html
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

import requests
import gspread
from google.oauth2.service_account import Credentials

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]
OSRM_BASE = "https://router.project-osrm.org/route/v1/driving/"
# Per OSM tile-usage policy, always send a descriptive User-Agent with real contact info.
HTTP_HEADERS = {"User-Agent": "ShellFOS-RouteMap/1.0 (contact: admin@yourcompany.example)"}

OUTPUT_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "routes")


# ---------------------------------------------------------------------------
# Sheets I/O
# ---------------------------------------------------------------------------
def open_sheet():
    sheet_id = os.environ["SHEET_ID"]
    creds_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    gc = gspread.authorize(creds)
    return gc.open_by_key(sheet_id)


def load_location_logs(spreadsheet):
    ws = spreadsheet.worksheet("LocationLogs")
    values = ws.get_all_values()
    if not values:
        return []
    headers = [str(h).strip() for h in values[0]]
    # Keep only the first occurrence of each non-blank header name,
    # so blank or duplicate header cells can't break the read.
    keep = {}
    for idx, h in enumerate(headers):
        if h and h not in keep:
            keep[h] = idx
    records = []
    for row in values[1:]:
        records.append({h: (row[i] if i < len(row) else "") for h, i in keep.items()})
    return records


def get_or_create_routemaps_sheet(spreadsheet):
    try:
        return spreadsheet.worksheet("RouteMaps")
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title="RouteMaps", rows=1000, cols=8)
        ws.append_row(
            ["UserID", "Date", "DistanceKm", "PointCount", "MapURL", "GeneratedAt", "Status"]
        )
        return ws


def upsert_route_row(ws, user_id, date_str, distance_km, point_count, map_url, status):
    existing = ws.get_all_values()
    header = existing[0] if existing else []
    row_idx = None
    for i, row in enumerate(existing[1:], start=2):
        if len(row) >= 2 and row[0] == user_id and row[1] == date_str:
            row_idx = i
            break
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    values = [user_id, date_str, round(distance_km, 2), point_count, map_url, generated_at, status]
    if row_idx:
        ws.update(f"A{row_idx}:G{row_idx}", [values])
    else:
        ws.append_row(values)


# ---------------------------------------------------------------------------
# Data prep
# ---------------------------------------------------------------------------
def norm_date(value):
    """LocationLogs Date cells may come back as 'yyyy-MM-dd' text already
    (gspread returns formatted display values by default), so just strip."""
    return str(value).strip()


def valid_point(row):
    try:
        lat = float(row.get("Lat", 0) or 0)
        lng = float(row.get("Lng", 0) or 0)
    except (TypeError, ValueError):
        return False
    if lat == 0 and lng == 0:
        return False  # "Location unknown" rows
    return True


def points_for(records, user_id, date_str):
    rows = [
        r for r in records
        if str(r.get("UserID", "")).strip() == user_id
        and norm_date(r.get("Date", "")) == date_str
        and valid_point(r)
    ]
    # Sort by Time (ISO timestamps sort correctly as strings; fall back to index otherwise)
    def sort_key(r):
        t = str(r.get("Time", ""))
        return t
    rows.sort(key=sort_key)
    return rows


def distinct_users_for_date(records, date_str):
    return sorted({str(r.get("UserID", "")).strip() for r in records if norm_date(r.get("Date", "")) == date_str})


def haversine_km(p1, p2):
    lat1, lon1 = p1
    lat2, lon2 = p2
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Routing (OSRM)
# ---------------------------------------------------------------------------
def osrm_route(coords_lonlat):
    """coords_lonlat: list of (lon, lat) tuples, in visit order.
    Returns (distance_km, geometry_lonlat_list) or (None, None) on failure."""
    coord_str = ";".join(f"{lon:.6f},{lat:.6f}" for lon, lat in coords_lonlat)
    url = f"{OSRM_BASE}{coord_str}"
    params = {"overview": "full", "geometries": "geojson"}
    try:
        resp = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            return None, None
        route = data["routes"][0]
        distance_km = route["distance"] / 1000.0
        geometry = route["geometry"]["coordinates"]  # [[lon,lat], ...]
        return distance_km, geometry
    except (requests.RequestException, ValueError, KeyError):
        return None, None


def fallback_straight_line(points_latlon):
    """points_latlon: list of (lat, lon) in order. Sums haversine distance
    and returns a straight-line 'geometry' for drawing when OSRM fails."""
    total = 0.0
    for a, b in zip(points_latlon, points_latlon[1:]):
        total += haversine_km(a, b)
    geometry = [[lon, lat] for lat, lon in points_latlon]
    return total, geometry


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>__TITLE__</title>
<link rel="stylesheet" href="../../vendor/leaflet/leaflet.css">
<style>
  html, body { height: 100%; margin: 0; font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif; }
  body { display: flex; flex-direction: column; }
  #bar { flex: 0 0 auto; background: #1a202c; color: #fff; padding: 8px 12px;
         padding-top: max(8px, env(safe-area-inset-top)); font-size: 14px; line-height: 1.35; }
  #bar b { font-size: 15px; }
  #bar .sub { opacity: .8; font-size: 12px; }
  #map { flex: 1 1 auto; min-height: 0; }
  .pin { width: 28px; height: 28px; border-radius: 50%; border: 3px solid #fff; color: #fff;
         font-weight: 700; font-size: 13px; line-height: 22px; text-align: center;
         box-shadow: 0 1px 5px rgba(0,0,0,.45); box-sizing: border-box; }
  .pop-t { color: #555; font-size: 12px; margin-top: 2px; }
  .pop-a { margin-top: 4px; }
  #fit { position: absolute; z-index: 1000; right: 10px; bottom: calc(24px + env(safe-area-inset-bottom));
         background: #fff; border: 2px solid rgba(0,0,0,.25); border-radius: 6px; padding: 8px 10px;
         font-size: 13px; font-weight: 600; }
</style>
</head>
<body>
<div id="bar"></div>
<div id="map"></div>
<button id="fit" type="button">Fit route</button>
<script src="../../vendor/leaflet/leaflet.js"></script>
<script>
(function () {
  var D = __DATA__;

  var bar = document.getElementById('bar');
  var title = document.createElement('div');
  var b = document.createElement('b');
  b.textContent = D.userId + '  \u2022  ' + D.date;
  title.appendChild(b);
  var sub = document.createElement('div');
  sub.className = 'sub';
  sub.textContent = D.distanceKm.toFixed(1) + ' km  \u2022  ' + D.points.length + ' points' +
                    (D.note ? '  (' + D.note + ')' : '');
  bar.appendChild(title);
  bar.appendChild(sub);

  var map = L.map('map', { zoomControl: true, maxZoom: 19 });
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors \u2014 route via OSRM'
  }).addTo(map);

  var all = [];
  if (D.route.length >= 2) {
    var latlngs = D.route.map(function (c) { return [c[1], c[0]]; });
    L.polyline(latlngs, { color: '#2b6cb0', weight: 5, opacity: 0.85 }).addTo(map);
    all = all.concat(latlngs);
  }

  var n = D.points.length;
  D.points.forEach(function (p, i) {
    var num = i + 1;
    var color = num === 1 ? '#22a34d' : (num === n ? '#d92b2b' : '#2b6cb0');
    var icon = L.divIcon({
      className: '',
      html: '<div class="pin" style="background:' + color + '">' + num + '</div>',
      iconSize: [28, 28],
      iconAnchor: [14, 14],
      popupAnchor: [0, -14]
    });
    var m = L.marker([p.lat, p.lng], {
      icon: icon,
      zIndexOffset: (num === 1 || num === n) ? 1000 : 0
    }).addTo(map);

    var box = document.createElement('div');
    var h = document.createElement('b');
    h.textContent = '#' + num + (num === 1 ? ' (start)' : (num === n ? ' (end)' : ''));
    box.appendChild(h);
    if (p.time) {
      var t = document.createElement('div');
      t.className = 'pop-t';
      t.textContent = p.time;
      box.appendChild(t);
    }
    if (p.address) {
      var a = document.createElement('div');
      a.className = 'pop-a';
      a.textContent = p.address;
      box.appendChild(a);
    }
    m.bindPopup(box);
    all.push([p.lat, p.lng]);
  });

  function fit() {
    if (all.length) { map.fitBounds(L.latLngBounds(all), { padding: [30, 30] }); }
    else { map.setView([20.5937, 78.9629], 5); }
  }
  fit();
  document.getElementById('fit').addEventListener('click', fit);
})();
</script>
</body>
</html>
"""


def build_map_page(points, geometry, distance_km, user_id, date_str, status_note=""):
    """points: list of dicts with Lat/Lng/Address/Time, in visit order.
    geometry: list of [lon, lat] describing the route line to draw.
    Returns the full HTML text of a self-contained interactive map page."""
    data = {
        "userId": str(user_id),
        "date": str(date_str),
        "distanceKm": round(float(distance_km), 3),
        "note": status_note,
        "points": [
            {
                "lat": round(float(p["Lat"]), 6),
                "lng": round(float(p["Lng"]), 6),
                "time": str(p.get("Time", "") or ""),
                "address": str(p.get("Address", "") or ""),
            }
            for p in points
        ],
        "route": [[round(lon, 5), round(lat, 5)] for lon, lat in geometry],
    }
    # "</" must not appear raw inside an inline <script> (e.g. from an address field)
    data_json = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    title = html.escape(f"{user_id} \u2022 {date_str} \u2022 {distance_km:.1f} km")
    return PAGE_TEMPLATE.replace("__TITLE__", title).replace("__DATA__", data_json)


# ---------------------------------------------------------------------------
# Main per-user-day pipeline
# ---------------------------------------------------------------------------
def process_user_day(records, user_id, date_str, pages_base_url):
    points = points_for(records, user_id, date_str)
    if len(points) < 2:
        print(f"  [skip] {user_id} {date_str}: only {len(points)} valid point(s), need >= 2")
        return None

    points_latlon = [(float(p["Lat"]), float(p["Lng"])) for p in points]
    coords_lonlat = [(lon, lat) for lat, lon in points_latlon]

    distance_km, geometry = osrm_route(coords_lonlat)
    status_note = ""
    if distance_km is None:
        print(f"  [warn] {user_id} {date_str}: OSRM routing failed, falling back to straight-line")
        distance_km, geometry = fallback_straight_line(points_latlon)
        status_note = "approx, straight-line"

    page = build_map_page(points, geometry, distance_km, user_id, date_str, status_note)

    out_dir = os.path.join(OUTPUT_ROOT, user_id)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{date_str}.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"  [ok] {user_id} {date_str}: {distance_km:.2f} km, {len(points)} points -> {out_path}")

    map_url = f"{pages_base_url.rstrip('/')}/routes/{user_id}/{date_str}.html"
    return {
        "user_id": user_id,
        "date": date_str,
        "distance_km": distance_km,
        "point_count": len(points),
        "map_url": map_url,
        "status": "ok" if not status_note else status_note,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="yyyy-MM-dd")
    parser.add_argument("--user-id", default="ALL", help="UserID, or ALL for every user with logs that date")
    args = parser.parse_args()

    pages_base_url = os.environ.get("PAGES_BASE_URL", "").strip()
    if not pages_base_url:
        print("ERROR: PAGES_BASE_URL environment variable is required.", file=sys.stderr)
        sys.exit(1)

    spreadsheet = open_sheet()
    records = load_location_logs(spreadsheet)
    routemaps_ws = get_or_create_routemaps_sheet(spreadsheet)

    if args.user_id.upper() == "ALL":
        user_ids = distinct_users_for_date(records, args.date)
        print(f"Processing ALL users for {args.date}: {user_ids}")
    else:
        user_ids = [args.user_id]

    results = []
    for uid in user_ids:
        result = process_user_day(records, uid, args.date, pages_base_url)
        if result:
            upsert_route_row(
                routemaps_ws,
                result["user_id"],
                result["date"],
                result["distance_km"],
                result["point_count"],
                result["map_url"],
                result["status"],
            )
            results.append(result)
        time.sleep(1)  # be polite to the public OSRM server

    print(f"\nDone. Generated {len(results)} route map(s).")


if __name__ == "__main__":
    main()
