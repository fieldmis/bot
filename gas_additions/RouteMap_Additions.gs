/**
 * ROUTE MAP ADDITIONS — paste into the existing Admin_Reporting-Code.gs
 *
 * 1. Add these two lines inside the doPost() switch statement, alongside
 *    the other admin panel actions:
 *
 *      case 'triggerRouteMap': result = handleTriggerRouteMap(payload); break;
 *      case 'getRouteMaps':    result = handleGetRouteMaps(payload); break;
 *
 * 2. Set three Script Properties (Project Settings > Script Properties):
 *      GITHUB_TOKEN  = a GitHub Personal Access Token with "repo" scope
 *                      (fine-grained token needs Contents: Read/Write and
 *                      "Repository dispatches" write on the target repo)
 *      GITHUB_OWNER  = your GitHub username or org
 *      GITHUB_REPO   = the repo name you pushed this workflow to
 *
 * 3. Paste the two functions below anywhere in Code.gs.
 *
 * The RouteMaps sheet tab is auto-created by the Python script the first
 * time it runs, with headers: UserID | Date | DistanceKm | PointCount |
 * ImageURL | GeneratedAt | Status
 */

/* ---------------- TRIGGER: kicks off the GitHub Action ---------------- */
function handleTriggerRouteMap(p) {
  const props = PropertiesService.getScriptProperties();
  const token = props.getProperty('GITHUB_TOKEN');
  const owner = props.getProperty('GITHUB_OWNER');
  const repo = props.getProperty('GITHUB_REPO');

  if (!token || !owner || !repo) {
    return { ok: false, message: 'GITHUB_TOKEN / GITHUB_OWNER / GITHUB_REPO script properties not set.' };
  }
  if (!p.date) {
    return { ok: false, message: 'A date (yyyy-MM-dd) is required.' };
  }

  const url = `https://api.github.com/repos/${owner}/${repo}/dispatches`;
  const options = {
    method: 'post',
    contentType: 'application/json',
    headers: {
      Authorization: 'token ' + token,
      Accept: 'application/vnd.github+json'
    },
    payload: JSON.stringify({
      event_type: 'generate_route_map',
      client_payload: {
        userId: p.userId || 'ALL',
        date: p.date
      }
    }),
    muteHttpExceptions: true
  };

  const resp = UrlFetchApp.fetch(url, options);
  const code = resp.getResponseCode();
  if (code === 204) {
    return { ok: true, message: 'Route map generation started. It usually takes 30\u201390 seconds \u2014 refresh in a bit.' };
  }
  return { ok: false, message: `GitHub API returned ${code}: ${resp.getContentText()}` };
}

/* ---------------- READ: lets the admin/user app list generated maps ---------------- */
function handleGetRouteMaps(p) {
  const sh = ss().getSheetByName('RouteMaps');
  if (!sh) return { ok: true, rows: [] };
  const data = sh.getDataRange().getValues();
  if (data.length < 2) return { ok: true, rows: [] };
  const headers = data.shift().map(h => String(h));

  const uidCol = findCol(headers, ['userid', 'user id']);
  const dateCol = findCol(headers, ['date']);
  const kmCol = findCol(headers, ['distancekm', 'distance km', 'distance']);
  const ptsCol = findCol(headers, ['pointcount', 'points']);
  const urlCol = findCol(headers, ['imageurl', 'image url']);
  const genCol = findCol(headers, ['generatedat', 'generated at']);
  const statusCol = findCol(headers, ['status']);

  let rows = data.map(row => ({
    userId: row[uidCol],
    date: row[dateCol],
    distanceKm: row[kmCol],
    points: row[ptsCol],
    imageUrl: row[urlCol],
    generatedAt: row[genCol],
    status: statusCol !== -1 ? row[statusCol] : ''
  }));

  if (p.userId) {
    rows = rows.filter(r => String(r.userId) === String(p.userId));
  }
  // Most recent first
  rows.sort((a, b) => String(b.date).localeCompare(String(a.date)));

  return { ok: true, rows: rows };
}
