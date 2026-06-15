"""
Tableau Pulse REST API client.
Resolved via discovery probes against bankingses-prod:
  - Site-wide listing supported (no datasource_id needed)
  - Temporality field: specification.temporality
  - Enum values: TEMPORALITY_OVER_TIME, TEMPORALITY_LATEST_POINT_IN_TIME
  - Update via PATCH with application/json
"""

import requests

TEMPORALITY_OVER_TIME = "TEMPORALITY_OVER_TIME"
TEMPORALITY_LATEST = "TEMPORALITY_LATEST_POINT_IN_TIME"


def list_all_definitions(base_url: str, auth_token: str, page_size: int = 100) -> list:
    """
    Return all Pulse definitions on the site (paginated).
    Each definition is the wrapped object returned by the API: a dict with metadata, specification, etc.
    """
    url = f"{base_url}/api/-/pulse/definitions"
    headers = {"x-tableau-auth": auth_token}
    all_defs = []
    params = {"page_size": page_size}

    while True:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        all_defs.extend(data.get("definitions", []))
        page_token = data.get("next_page_token")
        if not page_token:
            break
        params = {"page_size": page_size, "page_token": page_token}

    return all_defs


def get_definition(base_url: str, auth_token: str, definition_id: str) -> dict:
    """Return the full definition payload (wrapped under 'definition' key in response)."""
    url = f"{base_url}/api/-/pulse/definitions/{definition_id}"
    headers = {"x-tableau-auth": auth_token}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json().get("definition", {})


class PulseAPIError(Exception):
    """Carries HTTP status + response body so callers can show users a helpful message."""

    def __init__(self, status_code: int, body: str, message: str = ""):
        self.status_code = status_code
        self.body = body
        super().__init__(message or f"HTTP {status_code}: {body}")


def update_temporality(base_url: str, auth_token: str, definition_id: str, value: str) -> dict:
    """
    PATCH the Temporality field on a Pulse metric definition.
    `value` should be TEMPORALITY_OVER_TIME or TEMPORALITY_LATEST.
    Returns the updated definition body on success. Raises PulseAPIError on failure.
    """
    url = f"{base_url}/api/-/pulse/definitions/{definition_id}"
    headers = {
        "x-tableau-auth": auth_token,
        "Content-Type": "application/json",
    }
    body = {"specification": {"temporality": value}}
    resp = requests.patch(url, headers=headers, json=body, timeout=30)
    if not resp.ok:
        raise PulseAPIError(resp.status_code, resp.text)
    return resp.json().get("definition", {})


def extract_measure_field(definition: dict) -> str:
    """Return the measure field name from a definition payload."""
    return (
        definition.get("specification", {})
        .get("basic_specification", {})
        .get("measure", {})
        .get("field", "")
    )


def extract_aggregation(definition: dict) -> str:
    """Return the measure aggregation enum (e.g. AGGREGATION_SUM)."""
    return (
        definition.get("specification", {})
        .get("basic_specification", {})
        .get("measure", {})
        .get("aggregation", "")
    )


def extract_temporality(definition: dict) -> str:
    """Return the current temporality value, defaulting to TEMPORALITY_OVER_TIME if absent."""
    return definition.get("specification", {}).get("temporality", TEMPORALITY_OVER_TIME)


def get_default_metric_id(base_url: str, auth_token: str, definition_id: str) -> str:
    """
    Return the metric ID for the default (is_default=true) metric of a definition.
    Falls back to the first metric if none is flagged default.
    Returns empty string if no metrics exist.
    """
    url = f"{base_url}/api/-/pulse/definitions/{definition_id}/metrics"
    headers = {"x-tableau-auth": auth_token}
    resp = requests.get(url, headers=headers, timeout=30)
    if not resp.ok:
        return ""
    metrics = resp.json().get("metrics", [])
    if not metrics:
        return ""
    default = next((m for m in metrics if m.get("is_default")), metrics[0])
    return default.get("id", "")


def extract_datasource_id(definition: dict) -> str:
    """Return the datasource LUID from a definition payload."""
    return (
        definition.get("specification", {})
        .get("datasource", {})
        .get("id", "")
    )
