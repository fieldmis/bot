# SHELL FOS — Route Map Generator

Generates a per-user, per-day, road-following route map (PNG) from your
`LocationLogs` sheet, triggered by a button in your existing admin app.

**Flow:** Admin clicks "Generate Route Map" -> Apps Script calls GitHub's
API -> GitHub Actions runs a Python job that reads `LocationLogs`, routes
the day's points through OSRM (real roads, real km), draws a numbered
map, commits it to `docs/routes/<UserID>/<date>.png`, and logs the
result to a new `RouteMaps` sheet tab -> your admin app polls that tab
and shows the image.

No paid APIs. No servers to run — GitHub Actions + GitHub Pages host
everything.

## 1. Push this folder to a GitHub repo

Commit `.github/workflows/`, `scripts/`, `requirements.txt`, and the
(empty for now) `docs/routes/` folder to a repo — new or existing.

## 2. Enable GitHub Pages

Repo Settings → Pages → Source: **Deploy from a branch** → Branch:
`main`, folder: **/docs**. Save. Your images will then be reachable at
`https://<owner>.github.io/<repo>/routes/<UserID>/<date>.png`.

## 3. Create a Google service account and share the Sheet with it

1. In Google Cloud Console, create (or reuse) a project, enable the
   **Google Sheets API**, and create a **Service Account**.
2. Create a JSON key for it and download it.
3. Open your `LocationLogs` spreadsheet → Share → paste the service
   account's email (looks like `xyz@project.iam.gserviceaccount.com`)
   → give it **Editor** access (it needs to write the `RouteMaps` tab).

## 4. Add GitHub repo secrets

Repo Settings → Secrets and variables → Actions → New repository secret:

| Secret name | Value |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | the entire contents of the JSON key file from step 3 |
| `SHEET_ID` | `1xG4vwLMVDf1iTUMXiCktGZpuN6Dai3OUW0nvpq1fwXE` (your `SHEET_ID` from Code.gs) |

## 5. Create a GitHub token for Apps Script to call

Create a GitHub Personal Access Token (fine-grained, scoped to just this
repo, with **Contents: Read and write** — repository_dispatch needs
write access) or a classic token with the `repo` scope.

## 6. Wire up Apps Script

1. Open your existing `Admin_Reporting-Code.gs` project.
2. Paste in the two functions from `gas_additions/RouteMap_Additions.gs`,
   and add the two `case` lines into the `doPost()` switch (see comments
   at the top of that file).
3. Project Settings → Script Properties → add:
   - `GITHUB_TOKEN` = the token from step 5
   - `GITHUB_OWNER` = your GitHub username/org
   - `GITHUB_REPO` = the repo name from step 1
4. Redeploy the web app (Deploy → Manage deployments → Edit → New version).

## 7. Add the button to your admin panel

Paste `gas_additions/admin_route_map_snippet.html` into `admin.html`,
next to your other admin panels, and adjust it to match your existing
`CONFIG.API_URL` variable and CSS.

## 8. Test it

First test the workflow directly in GitHub (Actions tab → "Generate
Route Map" → Run workflow → enter a date, `UserID` or `ALL`) before
wiring the button, so you can see logs if anything's misconfigured.
Then test the button from the admin panel — allow ~30–90 seconds, then
refresh.

## Notes / limits

- **OSRM public server** is free with no key, fine for a manually
  triggered tool like this, but it's a shared demo service — don't wire
  it to run on every single log point in real time. Manual/nightly
  batch use, as set up here, is well within reasonable use.
- Rows where `Lat`/`Lng` are both `0` (your "Location unknown" rows) are
  automatically excluded from the route.
- If OSRM can't find a road route for a given set of points (rare — e.g.
  points too far apart or across water), the script falls back to a
  straight-line route and labels the image "approx, straight-line" so
  it's never silently wrong.
- Image size is 1600×1200 by default — edit `IMG_WIDTH`/`IMG_HEIGHT` in
  `scripts/generate_route_map.py` for a different resolution.
- `RouteMaps` sheet tab is auto-created on first run.
