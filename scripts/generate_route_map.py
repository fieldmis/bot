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
  4. Renders a high-resolution PNG: OSM basemap + road-following route
     line + numbered pin for every sequence point (green=start,
     red=end, blue=in-between) + a caption bar with user/date/distance.
  5. Saves the PNG to docs/routes/<UserID>/<date>.png (served by GitHub
     Pages once the repo has Pages enabled on the /docs folder).
  6. Upserts a row into the RouteMaps sheet tab so the backend app can
     look up the generated image URL + distance for that user/date.

Usage:
    python generate_route_map.py --date 2026-09-09 --user-id D91689
    python generate_route_map.py --date 2026-09-09              # all users that day

Required environment variables:
    SHEET_ID                        Google Sheet ID (LocationLogs / RouteMaps live here)
    GOOGLE_APPLICATION_CREDENTIALS  Path to a service-account JSON key file
    PAGES_BASE_URL                  e.g. https://<owner>.github.io/<repo>
                                     (used to build the ImageURL written to RouteMaps)
"""

import argparse
import math
import os
import sys
import time
from datetime import datetime, timezone

import requests
import gspread
from google.oauth2.service_account import Credentials
from PIL import Image, ImageDraw, ImageFont
from staticmap import StaticMap, Line, IconMarker

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]
OSRM_BASE = "https://router.project-osrm.org/route/v1/driving/"
TILE_URL_TEMPLATE = "https://a.tile.openstreetmap.org/{z}/{x}/{y}.png"
# Per OSM tile-usage policy, always send a descriptive User-Agent with real contact info.
HTTP_HEADERS = {"User-Agent": "ShellFOS-RouteMap/1.0 (contact: admin@yourcompany.example)"}

IMG_WIDTH = 1600
IMG_HEIGHT = 1200
CAPTION_HEIGHT = 70
ICON_SIZE = 34

OUTPUT_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "routes")
ICON_CACHE_DIR = "/tmp/route_icons"


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
    records = ws.get_all_records()  # list of dicts keyed by header row
    return records


def get_or_create_routemaps_sheet(spreadsheet):
    try:
        return spreadsheet.worksheet("RouteMaps")
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title="RouteMaps", rows=1000, cols=8)
        ws.append_row(
            ["UserID", "Date", "DistanceKm", "PointCount", "ImageURL", "GeneratedAt", "Status"]
        )
        return ws


def upsert_route_row(ws, user_id, date_str, distance_km, point_count, image_url, status):
    existing = ws.get_all_values()
    header = existing[0] if existing else []
    row_idx = None
    for i, row in enumerate(existing[1:], start=2):
        if len(row) >= 2 and row[0] == user_id and row[1] == date_str:
            row_idx = i
            break
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    values = [user_id, date_str, round(distance_km, 2), point_count, image_url, generated_at, status]
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
def make_numbered_icon(number, color):
    os.makedirs(ICON_CACHE_DIR, exist_ok=True)
    path = os.path.join(ICON_CACHE_DIR, f"icon_{number}_{color.lstrip('#')}.png")
    if os.path.exists(path):
        return path
    img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([1, 1, ICON_SIZE - 2, ICON_SIZE - 2], fill=color, outline="white", width=3)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 15)
    except OSError:
        font = ImageFont.load_default()
    text = str(number)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((ICON_SIZE - tw) / 2, (ICON_SIZE - th) / 2 - bbox[1]), text, fill="white", font=font)
    img.save(path)
    return path


def render_map(points, geometry, distance_km, user_id, date_str, status_note=""):
    """points: list of dicts with Lat/Lng/Address/Time, in visit order.
    geometry: list of [lon, lat] describing the route line to draw."""
    m = StaticMap(IMG_WIDTH, IMG_HEIGHT, url_template=TILE_URL_TEMPLATE, headers=HTTP_HEADERS)

    line_coords = [(lon, lat) for lon, lat in geometry]
    if len(line_coords) >= 2:
        m.add_line(Line(line_coords, "#2b6cb0", 5))

    n = len(points)
    for i, p in enumerate(points, start=1):
        lat, lng = float(p["Lat"]), float(p["Lng"])
        if i == 1:
            color = "#22a34d"  # start = green
        elif i == n:
            color = "#d92b2b"  # end = red
        else:
            color = "#2b6cb0"  # waypoint = blue
        icon_path = make_numbered_icon(i, color)
        m.add_marker(IconMarker((lng, lat), icon_path, ICON_SIZE // 2, ICON_SIZE // 2))

    base_img = m.render()

    # Compose final image with a caption bar on top
    final = Image.new("RGB", (IMG_WIDTH, IMG_HEIGHT + CAPTION_HEIGHT), "white")
    final.paste(base_img, (0, CAPTION_HEIGHT))
    draw = ImageDraw.Draw(final)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
        small_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
    except OSError:
        font = ImageFont.load_default()
        small_font = font

    caption = f"{user_id}  \u2022  {date_str}  \u2022  {distance_km:.1f} km  \u2022  {n} points"
    if status_note:
        caption += f"  ({status_note})"
    draw.text((16, 18), caption, fill="black", font=font)
    draw.text(
        (16, IMG_HEIGHT + CAPTION_HEIGHT - 20),
        "\u00a9 OpenStreetMap contributors \u2014 route via OSRM",
        fill="#555555",
        font=small_font,
    )
    return final


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

    img = render_map(points, geometry, distance_km, user_id, date_str, status_note)

    out_dir = os.path.join(OUTPUT_ROOT, user_id)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{date_str}.png")
    img.save(out_path, "PNG")
    print(f"  [ok] {user_id} {date_str}: {distance_km:.2f} km, {len(points)} points -> {out_path}")

    image_url = f"{pages_base_url.rstrip('/')}/routes/{user_id}/{date_str}.png"
    return {
        "user_id": user_id,
        "date": date_str,
        "distance_km": distance_km,
        "point_count": len(points),
        "image_url": image_url,
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
                result["image_url"],
                result["status"],
            )
            results.append(result)
        time.sleep(1)  # be polite to the public OSRM/OSM tile servers

    print(f"\nDone. Generated {len(results)} route map(s).")


if __name__ == "__main__":
    main()
