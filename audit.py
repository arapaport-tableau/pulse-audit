"""
PulseAudit classification engine.

Classifies each Pulse metric definition into one of 4 buckets:
  ALREADY_CONVERTED  — temporality already set to TEMPORALITY_LATEST_POINT_IN_TIME
  CONVERT_CANDIDATE  — measure formula uses EXCLUDE LOD (the canonical Pulse snapshot workaround)
  NOT_APPLICABLE     — no EXCLUDE signal found
  ERROR              — couldn't classify (schema fetch failed, etc.)
"""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

from pulse_api import (
    TEMPORALITY_LATEST,
    extract_aggregation,
    extract_datasource_id,
    extract_measure_field,
    extract_temporality,
)
from tableau_client import filter_schema, get_schema

BUCKET_ALREADY = "ALREADY_CONVERTED"
BUCKET_HIGH = "CONVERT_CANDIDATE"
BUCKET_NOT_APPLICABLE = "NOT_APPLICABLE"
BUCKET_ERROR = "ERROR"

BUCKET_LABELS = {
    BUCKET_HIGH: "Convert Candidate",
    BUCKET_ALREADY: "Already Converted",
    BUCKET_NOT_APPLICABLE: "Not Applicable",
    BUCKET_ERROR: "Error",
}

# Display priority — actionable first, then converted, then noise
BUCKET_PRIORITY = {
    BUCKET_HIGH: 0,
    BUCKET_ALREADY: 1,
    BUCKET_NOT_APPLICABLE: 2,
    BUCKET_ERROR: 3,
}


def sort_rows(rows: List[dict]) -> List[dict]:
    """Sort by bucket priority, then alphabetical name."""
    return sorted(rows, key=lambda r: (BUCKET_PRIORITY.get(r.get("bucket", ""), 99), (r.get("name") or "").lower()))


# Only EXCLUDE is the documented Pulse snapshot workaround pattern.
# FIXED appears in date-shifting helpers (demo data) and INCLUDE is unrelated.
EXCLUDE_PATTERN = re.compile(r"\bEXCLUDE\b", re.IGNORECASE)
ADVANCED_DEF_PATTERN = re.compile(r"^Calculation_\d+$")


FIELD_TYPE_CALCULATED = "calculated"
FIELD_TYPE_COLUMN = "column"
FIELD_TYPE_ADVANCED_DEF = "advanced_definition"
FIELD_TYPE_UNKNOWN = "unknown"


def find_field(schema: list, field_name: str) -> Optional[dict]:
    """Return the schema entry for a field by name, or None."""
    for f in schema:
        if f.get("name") == field_name:
            return f
    return None


def classify_definition(
    definition: dict,
    schema_lookup: Dict[str, list],
    schema_errors: Dict[str, str],
    datasource_names: Optional[Dict[str, str]] = None,
    datasource_metadata_names: Optional[Dict[str, str]] = None,
    calc_maps: Optional[Dict[str, Dict[str, dict]]] = None,
) -> dict:
    """
    Classify a single definition.
    Flags only metrics whose measure formula uses EXCLUDE LOD — the documented Pulse snapshot workaround.
    """
    name = definition.get("metadata", {}).get("name", "")
    defn_id = definition.get("metadata", {}).get("id", "")
    ds_id = extract_datasource_id(definition)
    measure_field = extract_measure_field(definition)
    aggregation = extract_aggregation(definition)
    temporality = extract_temporality(definition)

    ds_name = ""
    if datasource_names and ds_id in datasource_names:
        ds_name = datasource_names[ds_id]
    elif datasource_metadata_names and ds_id in datasource_metadata_names:
        ds_name = datasource_metadata_names[ds_id]

    result = {
        "id": defn_id,
        "name": name,
        "datasource_id": ds_id,
        "datasource_name": ds_name,
        "measure_field": measure_field,
        "measure_caption": "",
        "aggregation": aggregation,
        "current_temporality": temporality,
        "bucket": BUCKET_NOT_APPLICABLE,
        "reasoning": [],
        "formula": None,
        "field_type": FIELD_TYPE_UNKNOWN,
        "is_advanced_definition": False,
    }

    # Resolve the measure formula
    schema = schema_lookup.get(ds_id)
    schema_failed = ds_id and schema is None
    is_advanced_def = bool(measure_field and ADVANCED_DEF_PATTERN.match(measure_field))

    if is_advanced_def:
        result["is_advanced_definition"] = True
        result["field_type"] = FIELD_TYPE_ADVANCED_DEF
        ds_calc_map = (calc_maps or {}).get(ds_id) or {}
        entry = ds_calc_map.get(measure_field)
        if entry:
            result["measure_caption"] = entry.get("caption", "")
            result["formula"] = entry.get("formula", "")
    elif schema is not None and measure_field:
        field_entry = find_field(schema, measure_field)
        if field_entry:
            if field_entry.get("__typename") == "CalculatedField":
                result["field_type"] = FIELD_TYPE_CALCULATED
                result["formula"] = field_entry.get("formula", "") or ""
            else:
                result["field_type"] = FIELD_TYPE_COLUMN

    # Already converted — no action needed
    if temporality == TEMPORALITY_LATEST:
        result["bucket"] = BUCKET_ALREADY
        result["reasoning"].append("Temporality is already set to Latest point in time.")
        return result

    # Schema fetch failed — can't inspect the formula
    if schema_failed:
        err = schema_errors.get(ds_id, "Unknown error")
        result["bucket"] = BUCKET_ERROR
        result["reasoning"].append(f"Could not fetch datasource schema: {err}")
        return result

    # Convert Candidate — measure formula contains EXCLUDE LOD
    if result["formula"] and EXCLUDE_PATTERN.search(result["formula"]):
        caption = result.get("measure_caption") or measure_field
        if is_advanced_def:
            result["reasoning"].append(
                f"Measure is a Pulse Advanced Definition (calculated field \"{caption}\") "
                "that uses EXCLUDE LOD — the snapshot workaround the Temporality setting replaces."
            )
        else:
            result["reasoning"].append(
                f"Measure formula uses EXCLUDE LOD — the snapshot workaround the Temporality setting replaces."
            )
        result["bucket"] = BUCKET_HIGH
        return result

    # Advanced Definition but formula not available (TDSx download failed)
    if is_advanced_def and not result["formula"]:
        result["bucket"] = BUCKET_NOT_APPLICABLE
        result["reasoning"].append(
            f"Measure is a Pulse Advanced Definition ({measure_field}) but the datasource could not be "
            "downloaded to inspect the formula. Check in Tableau whether it uses EXCLUDE LOD."
        )
        return result

    # Not applicable
    result["bucket"] = BUCKET_NOT_APPLICABLE
    if result["field_type"] == FIELD_TYPE_CALCULATED:
        result["reasoning"].append("Measure formula does not contain EXCLUDE LOD. Not a snapshot workaround.")
    elif result["field_type"] == FIELD_TYPE_COLUMN:
        result["reasoning"].append("Measure is a regular column with no formula.")
    else:
        result["reasoning"].append("No EXCLUDE LOD found in measure formula.")
    return result


def fetch_schemas_parallel(
    base_url: str,
    auth_token: str,
    datasource_ids: List[str],
    max_workers: int = 4,
) -> tuple:
    """
    Fetch schemas for a set of datasource IDs in parallel.
    Returns (schema_lookup, schema_errors, metadata_names).
    schema_lookup maps ds_id → list of fields (None on failure).
    metadata_names maps ds_id → datasource name from Metadata API (used as fallback for naming).
    """
    schema_lookup = {}
    schema_errors = {}
    metadata_names = {}

    def fetch_one(ds_id):
        try:
            schema = get_schema(base_url, auth_token, ds_id)
            return ds_id, schema, None
        except Exception as e:
            return ds_id, None, str(e)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(fetch_one, ds_id) for ds_id in datasource_ids]
        for fut in as_completed(futures):
            ds_id, schema, err = fut.result()
            if err is not None:
                schema_errors[ds_id] = err
                schema_lookup[ds_id] = None
            else:
                schema_lookup[ds_id] = filter_schema(schema.get("fields", []))
                if schema.get("name"):
                    metadata_names[ds_id] = schema["name"]

    return schema_lookup, schema_errors, metadata_names


def run_audit(
    base_url: str,
    auth_token: str,
    definitions: List[dict],
    datasource_names: Optional[Dict[str, str]] = None,
    calc_maps: Optional[Dict[str, Dict[str, dict]]] = None,
) -> dict:
    """
    Classify all definitions. Returns a dict with rows + summary.
    `datasource_names` maps LUID → name (from TSC).
    `calc_maps` maps datasource LUID → {Calculation_xxx: {caption, formula, raw_formula}} from TDSx parse.
    """
    unique_datasource_ids = list({extract_datasource_id(d) for d in definitions if extract_datasource_id(d)})
    schema_lookup, schema_errors, metadata_names = fetch_schemas_parallel(
        base_url, auth_token, unique_datasource_ids
    )

    rows = []
    for d in definitions:
        try:
            row = classify_definition(
                d, schema_lookup, schema_errors,
                datasource_names=datasource_names,
                datasource_metadata_names=metadata_names,
                calc_maps=calc_maps,
            )
        except Exception as e:
            row = {
                "id": d.get("metadata", {}).get("id", ""),
                "name": d.get("metadata", {}).get("name", "?"),
                "datasource_id": extract_datasource_id(d),
                "datasource_name": (datasource_names or {}).get(extract_datasource_id(d), ""),
                "measure_field": extract_measure_field(d),
                "measure_caption": "",
                "aggregation": extract_aggregation(d),
                "current_temporality": extract_temporality(d),
                "bucket": BUCKET_ERROR,
                "reasoning": [f"Classifier error: {e}"],
                "formula": None,
                "field_type": FIELD_TYPE_UNKNOWN,
                "is_advanced_definition": False,
            }
        rows.append(row)

    rows = sort_rows(rows)

    counts = {bucket: 0 for bucket in BUCKET_LABELS}
    for r in rows:
        counts[r["bucket"]] = counts.get(r["bucket"], 0) + 1

    return {
        "rows": rows,
        "counts": counts,
        "total": len(rows),
        "schema_errors": schema_errors,
    }
