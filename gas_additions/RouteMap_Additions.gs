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
 * MapURL | GeneratedAt | Status
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
  const date = String(p.date || '').trim();
  const userId = String(p.userId || 'ALL').trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
    return { ok: false, message: 'A date in yyyy-MM-dd format is required.' };
  }
  // 'ALL' or a comma-separated list of UserIDs, e.g. D91689,D69561 (letters, digits, _ and - only)
  if (!/^[A-Za-z0-9_,\-]{1,4000}$/.test(userId)) {
    return { ok: false, message: 'Invalid user list.' };
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
        userId: userId,
        date: date
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
  const urlCol = findCol(headers, ['mapurl', 'map url', 'imageurl', 'image url']);
  const genCol = findCol(headers, ['generatedat', 'generated at']);
  const statusCol = findCol(headers, ['status']);

  // Sheets can hand back real Date objects for date-looking cells; normalise to text
  // so the admin app always receives 'yyyy-MM-dd' and an ISO timestamp.
  const tz = Session.getScriptTimeZone();
  const fmt = (v, pattern) => (v instanceof Date ? Utilities.formatDate(v, tz, pattern) : v);

  let rows = data.map(row => ({
    userId: row[uidCol],
    date: fmt(row[dateCol], 'yyyy-MM-dd'),
    distanceKm: row[kmCol],
    points: row[ptsCol],
    mapUrl: row[urlCol],
    imageUrl: row[urlCol], // kept so older admin-app code keeps working; this is now a map PAGE link
    generatedAt: fmt(row[genCol], "yyyy-MM-dd'T'HH:mm:ssXXX"),
    status: statusCol !== -1 ? row[statusCol] : ''
  }));

  if (p.userId) {
    rows = rows.filter(r => String(r.userId) === String(p.userId));
  }
  // Most recent first
  rows.sort((a, b) => String(b.date).localeCompare(String(a.date)));

  return { ok: true, rows: rows };
}
