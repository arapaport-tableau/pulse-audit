# PulseAudit

Find the Tableau Pulse metrics that need the new "Latest point in time" Temporality setting, and convert them inline.

PulseAudit is a small local web tool that connects to a Tableau Cloud site, lists every Pulse metric, classifies which ones likely need conversion to a snapshot metric, and lets a Pulse admin flip the Temporality setting from the browser. PAT lives in memory only. No telemetry, no database, no LLM.

Companion to [How Pulse Handles Point-in-Time Metrics Now](https://andyrapaport.com/article-pit-metrics.html).

## What it detects

Each Pulse metric on the site lands in one of four buckets:

| Bucket | Trigger |
|---|---|
| **Convert Candidate** | The measure formula contains an `EXCLUDE` LOD expression — the documented Pulse snapshot workaround |
| **Already Converted** | `specification.temporality` is already `TEMPORALITY_LATEST_POINT_IN_TIME` |
| **Not Applicable** | No `EXCLUDE` LOD found in the measure formula — likely a flow or rate metric |
| **Error** | Datasource schema could not be fetched (Metadata API permission denied or network failure) |

Detection is formula-based only. Name-based heuristics were removed to eliminate false positives. If a metric's measure field is an Advanced Definition (`Calculation_*`), PulseAudit downloads the datasource to resolve the actual formula.

## Quick start

```bash
git clone https://github.com/arapaport-tableau/pulse-audit.git
cd pulse-audit
./run.sh
# open http://localhost:5050
```

`run.sh` creates a Python virtual environment, installs three packages (`flask`, `tableauserverclient`, `requests`), and starts the app.

### Manual install

If you prefer not to run the shell script:

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python3 app.py
```

Requires Python 3.10+.

## Sign in

PulseAudit asks for four things:

- **Server URL** — your Tableau Cloud pod hostname (e.g. `prod-uswest-c.online.tableau.com`)
- **Site Content URL** — the site identifier (the part after `/site/` in the URL)
- **PAT Name** — a Personal Access Token name (create one in your Tableau Cloud account settings)
- **PAT Secret** — the secret value shown once when the PAT was created

The PAT needs:
- **Pulse API access** to list and update metric definitions
- **Metadata API access** to inspect calculated field formulas (used for High Confidence detection)

A read-only PAT can audit but won't be able to convert. Tick the "Read-only mode" checkbox on the login form to hide Convert buttons.

## What it does

1. Calls `GET /api/-/pulse/definitions` and pages through every Pulse metric on the site
2. For each unique datasource, queries the Metadata API and (for Advanced Definition metrics) downloads the datasource to resolve calculated field formulas. Schema fetches are parallelized and cached per session.
3. Classifies each metric using the rules above
4. Renders a filterable table with bucket badges, search, and per-row Convert / Revert actions
5. Convert clicks fire `PATCH /api/-/pulse/definitions/{id}` with `{"specification": {"temporality": "TEMPORALITY_LATEST_POINT_IN_TIME"}}`
6. CSV export is one click — full audit data including formula text and reasoning

## Privacy and security

- The PAT secret is never written to disk by PulseAudit. It lives in the Flask session in process memory.
- The Flask session cookie is signed with a random key generated at process start. Logging out, restarting the process, or losing the cookie clears auth state.
- No analytics. No phone-home. No external services. The tool talks only to your Tableau Cloud site.
- Source is MIT-licensed. Read it before running.

If you want to host PulseAudit on an internal server for your team:
- Set `PULSE_AUDIT_SECRET_KEY` to a stable random value so sessions survive restarts
- Bind to `0.0.0.0` and put it behind a reverse proxy with your org's SSO
- Each user still enters their own PAT — there is no shared service account

## Limitations

- Tableau Cloud only. No on-prem Tableau Server support.
- Single site per session. To audit another site, log out and log back in.
- Bulk convert isn't in MVP. Per-row conversion only.
- Conversion is not undoable beyond clicking Revert. The CSV export captures pre-conversion temporality so you can track changes.
- The Metadata API only exposes formulas for published datasources. Metrics built on embedded datasources may fall through to Not Applicable even if they use the LOD workaround.
- Some metrics may fail to convert via the API with a generic 400 error. In those cases PulseAudit surfaces an "Open in Pulse ↗" link to complete the conversion manually from the Tableau UI.

## License

MIT — see [LICENSE](LICENSE).
