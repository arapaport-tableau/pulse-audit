"""
PulseAudit — Flask app.

Runs locally on http://localhost:5000.
Authentication: PAT entered at runtime, never persisted.
"""

import csv
import io
import os
import secrets
from datetime import datetime

from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for

from audit import BUCKET_LABELS, classify_definition, run_audit
from pulse_api import (
    PulseAPIError,
    TEMPORALITY_LATEST,
    TEMPORALITY_OVER_TIME,
    extract_aggregation,
    extract_datasource_id,
    extract_temporality,
    get_default_metric_id,
    list_all_definitions,
    update_temporality,
)
from concurrent.futures import ThreadPoolExecutor

from tableau_client import (
    connect,
    download_datasource_calc_map,
    filter_schema,
    get_schema,
    list_datasource_names,
)

ADVANCED_DEF_RE = __import__("re").compile(r"^Calculation_\d+$")

app = Flask(__name__)
app.secret_key = os.environ.get("PULSE_AUDIT_SECRET_KEY", secrets.token_hex(32))
app.config["TEMPLATES_AUTO_RELOAD"] = True

# In-memory storage for the audit results, keyed by session id.
# Not pickled into the cookie — too large.
_AUDIT_CACHE = {}


def _connection_from_session():
    """Pull connection info from session, or None if not signed in."""
    if "auth_token" not in session:
        return None
    return {
        "base_url": session["base_url"],
        "auth_token": session["auth_token"],
        "site_name": session.get("site_name", ""),
        "site_id": session.get("site_id", ""),
        "user_id": session.get("user_id", ""),
        "read_only": session.get("read_only", False),
    }


def _session_key():
    sid = session.get("sid")
    if not sid:
        sid = secrets.token_hex(16)
        session["sid"] = sid
    return sid


@app.route("/")
def index():
    if _connection_from_session():
        return redirect(url_for("audit"))
    return render_template("login.html", error=None)


@app.route("/connect", methods=["POST"])
def do_connect():
    server_url = request.form.get("server_url", "").strip()
    site_name = request.form.get("site_name", "").strip()
    pat_name = request.form.get("pat_name", "").strip()
    pat_secret = request.form.get("pat_secret", "")
    read_only = request.form.get("read_only") == "on"

    if not all([server_url, site_name, pat_name, pat_secret]):
        return render_template("login.html", error="All fields are required."), 400

    try:
        ctx = connect(server_url, site_name, pat_name, pat_secret)
    except Exception as e:
        return render_template("login.html", error=f"Sign-in failed: {e}"), 401

    # Resolve datasource names while we have the TSC server object handy
    datasource_names = list_datasource_names(ctx["server"])

    sid = secrets.token_hex(16)
    session.clear()
    session["sid"] = sid
    session["base_url"] = ctx["base_url"]
    session["auth_token"] = ctx["auth_token"]
    session["site_name"] = ctx["site_name"]
    session["site_id"] = ctx["site_id"]
    session["user_id"] = ctx["user_id"] or ""
    session["read_only"] = read_only

    # Stash datasource names + TSC server in the in-memory cache for this session
    # (server is needed for downloading datasource TDSx files for calc-field resolution)
    _AUDIT_CACHE[sid] = {
        "datasource_names": datasource_names,
        "server": ctx["server"],
        "calc_maps": {},
    }
    return redirect(url_for("audit"))


def _ensure_calc_maps(sid: str, definitions: list) -> dict:
    """Download TDSx files for any datasource referenced by an Advanced Definition metric, parse calc fields. Cached per session."""
    cached = _AUDIT_CACHE.get(sid) or {}
    calc_maps = cached.get("calc_maps", {})
    server = cached.get("server")
    if not server:
        return calc_maps

    # Find datasource IDs that have at least one Advanced Definition metric
    needed_ds_ids = set()
    for d in definitions:
        measure = (d.get("specification", {}).get("basic_specification", {}).get("measure", {}).get("field", "") or "")
        if measure and ADVANCED_DEF_RE.match(measure):
            ds_id = d.get("specification", {}).get("datasource", {}).get("id", "")
            if ds_id and ds_id not in calc_maps:
                needed_ds_ids.add(ds_id)

    if not needed_ds_ids:
        return calc_maps

    def fetch(ds_id):
        return ds_id, download_datasource_calc_map(server, ds_id)

    with ThreadPoolExecutor(max_workers=4) as pool:
        for ds_id, m in pool.map(fetch, needed_ds_ids):
            calc_maps[ds_id] = m

    cached["calc_maps"] = calc_maps
    _AUDIT_CACHE[sid] = cached
    return calc_maps


@app.route("/audit")
def audit():
    conn = _connection_from_session()
    if not conn:
        return redirect(url_for("index"))

    sid = _session_key()
    cached = _AUDIT_CACHE.get(sid) or {}
    datasource_names = cached.get("datasource_names", {})

    if "rows" not in cached or request.args.get("refresh") == "1":
        try:
            defs = list_all_definitions(conn["base_url"], conn["auth_token"])
        except Exception as e:
            return render_template("login.html", error=f"Listing definitions failed: {e}"), 500

        # Resolve calc-field formulas for Advanced Definition metrics
        calc_maps = _ensure_calc_maps(sid, defs)

        result = run_audit(
            conn["base_url"], conn["auth_token"], defs,
            datasource_names=datasource_names,
            calc_maps=calc_maps,
        )
        result["datasource_names"] = datasource_names
        result["calc_maps"] = calc_maps
        # Preserve TSC server reference across the cache update
        result["server"] = cached.get("server")
        _AUDIT_CACHE[sid] = result
        cached = result

    return render_template(
        "results.html",
        site_name=conn["site_name"],
        read_only=conn["read_only"],
        rows=cached["rows"],
        counts=cached["counts"],
        total=cached["total"],
        bucket_labels=BUCKET_LABELS,
        schema_errors=cached["schema_errors"],
    )


@app.route("/convert/<defn_id>", methods=["POST"])
def convert(defn_id):
    conn = _connection_from_session()
    if not conn:
        return jsonify({"error": "Not signed in"}), 401
    if conn["read_only"]:
        return jsonify({"error": "Read-only mode is enabled"}), 403

    target = request.json.get("target", TEMPORALITY_LATEST) if request.is_json else TEMPORALITY_LATEST
    if target not in (TEMPORALITY_LATEST, TEMPORALITY_OVER_TIME):
        return jsonify({"error": f"Invalid target temporality: {target}"}), 400

    try:
        updated = update_temporality(conn["base_url"], conn["auth_token"], defn_id, target)
    except PulseAPIError as e:
        hint = ""
        if e.status_code == 400:
            hint = (
                " Pulse rejected this combination of fields without explanation. The metric may have a "
                "configuration that isn't compatible with the requested Temporality. Try changing it from "
                "the Pulse UI to see a more specific error."
            )
        return jsonify({
            "error": f"Tableau Pulse rejected the change (HTTP {e.status_code}).{hint}",
            "raw_body": e.body[:500],
        }), 502
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {e}"}), 500

    # Re-classify this single row in place
    sid = _session_key()
    cached = _AUDIT_CACHE.get(sid)
    new_row = None
    if cached:
        ds_id = extract_datasource_id(updated)
        schema_lookup = {}
        schema_errors = {}
        metadata_names = {}
        try:
            schema = get_schema(conn["base_url"], conn["auth_token"], ds_id)
            schema_lookup[ds_id] = filter_schema(schema.get("fields", []))
            if schema.get("name"):
                metadata_names[ds_id] = schema["name"]
        except Exception as e:
            schema_lookup[ds_id] = None
            schema_errors[ds_id] = str(e)

        new_row = classify_definition(
            updated, schema_lookup, schema_errors,
            datasource_names=cached.get("datasource_names", {}),
            datasource_metadata_names=metadata_names,
            calc_maps=cached.get("calc_maps", {}),
        )
        for i, r in enumerate(cached["rows"]):
            if r["id"] == defn_id:
                cached["rows"][i] = new_row
                break
        # Update bucket counts
        counts = {bucket: 0 for bucket in BUCKET_LABELS}
        for r in cached["rows"]:
            counts[r["bucket"]] = counts.get(r["bucket"], 0) + 1
        cached["counts"] = counts

    return jsonify({
        "ok": True,
        "id": defn_id,
        "new_temporality": extract_temporality(updated),
        "row": new_row if cached else None,
    })


@app.route("/export.csv")
def export_csv():
    conn = _connection_from_session()
    if not conn:
        return redirect(url_for("index"))

    sid = _session_key()
    cached = _AUDIT_CACHE.get(sid)
    if not cached:
        return redirect(url_for("audit"))

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "name", "definition_id", "datasource_id", "measure_field",
        "current_temporality", "bucket", "is_advanced_definition",
        "keyword_matches", "formula", "reasoning",
    ])
    for r in cached["rows"]:
        writer.writerow([
            r.get("name", ""),
            r.get("id", ""),
            r.get("datasource_id", ""),
            r.get("measure_field", ""),
            r.get("current_temporality", ""),
            BUCKET_LABELS.get(r.get("bucket", ""), r.get("bucket", "")),
            "yes" if r.get("is_advanced_definition") else "no",
            ", ".join(r.get("keyword_matches", [])),
            (r.get("formula") or "")[:1000],
            " | ".join(r.get("reasoning", [])),
        ])
    output.seek(0)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M")
    filename = f"pulse-audit-{conn['site_name']}-{timestamp}.csv"
    return send_file(
        io.BytesIO(output.getvalue().encode("utf-8")),
        mimetype="text/csv",
        as_attachment=True,
        download_name=filename,
    )



@app.route("/pulse_url/<defn_id>")
def pulse_url(defn_id):
    """Return the Pulse UI URL for the default metric of a definition."""
    conn = _connection_from_session()
    if not conn:
        return jsonify({"error": "Not signed in"}), 401
    metric_id = get_default_metric_id(conn["base_url"], conn["auth_token"], defn_id)
    if not metric_id:
        return jsonify({"error": "No metrics found for this definition"}), 404
    # Build the Pulse UI URL: https://{host}/pulse/site/{site_name}/metrics/{metric_id}
    host = conn["base_url"].replace("https://", "").replace("http://", "")
    url = f"https://{host}/pulse/site/{conn['site_name']}/metrics/{metric_id}"
    return jsonify({"url": url})


@app.route("/logout", methods=["POST", "GET"])
def logout():
    sid = session.get("sid")
    if sid:
        _AUDIT_CACHE.pop(sid, None)
    session.clear()
    return redirect(url_for("index"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5050"))
    print("=" * 60)
    print(f"PulseAudit running at http://localhost:{port}")
    print("Press Ctrl+C to stop.")
    print("=" * 60)
    app.run(host="127.0.0.1", port=port, debug=False)
