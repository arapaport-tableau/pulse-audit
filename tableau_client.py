"""
Tableau Cloud connection helpers and Metadata API access.
Adapted from pulse-metric-advisor/tableau_client.py.
"""

import html
import os
import re
import tempfile
import zipfile

import requests
import tableauserverclient as TSC


def normalize_url(server_url: str) -> str:
    server_url = server_url.strip().rstrip("/")
    if not server_url.startswith("http"):
        server_url = f"https://{server_url}"
    return server_url


def connect(server_url: str, site_name: str, pat_name: str, pat_secret: str) -> dict:
    base_url = normalize_url(server_url)
    auth = TSC.PersonalAccessTokenAuth(pat_name, pat_secret, site_id=site_name)
    server = TSC.Server(base_url, use_server_version=True)
    server.auth.sign_in(auth)
    return {
        "server": server,
        "auth_token": server.auth_token,
        "site_id": server.site_id,
        "site_name": site_name,
        "base_url": base_url,
        "user_id": server.user_id,
    }


def get_schema(base_url: str, auth_token: str, datasource_luid: str) -> dict:
    """
    Query the Metadata API for a published datasource.
    Returns {'name': str, 'project': str, 'fields': list}.
    Calculated fields include 'formula'.
    Raises ValueError on GraphQL errors (e.g. permission denied).
    """
    url = f"{base_url}/api/metadata/graphql"
    headers = {
        "x-tableau-auth": auth_token,
        "Content-Type": "application/json",
    }
    query = {
        "query": f"""
        {{
          publishedDatasourcesConnection(filter: {{luid: "{datasource_luid}"}}) {{
            nodes {{
              name
              projectName
              fields {{
                __typename
                name
                description
                isHidden
                ... on ColumnField {{ dataType }}
                ... on CalculatedField {{ dataType formula }}
                ... on GroupField {{ dataType }}
                ... on BinField {{ dataType }}
              }}
            }}
          }}
        }}
        """
    }
    resp = requests.post(url, headers=headers, json=query, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if data.get("errors"):
        messages = [e.get("message", str(e)) for e in data["errors"]]
        raise ValueError(f"Metadata API error: {'; '.join(messages)}")

    connection = (data.get("data") or {}).get("publishedDatasourcesConnection") or {}
    nodes = connection.get("nodes") or []
    if not nodes:
        return {"name": "", "project": "", "fields": []}
    node = nodes[0]
    return {
        "name": node.get("name", "") or "",
        "project": node.get("projectName", "") or "",
        "fields": node.get("fields", []) or [],
    }


def list_datasource_names(server) -> dict:
    """
    Return {luid: name} for every datasource on the site visible to the PAT user.
    Uses TSC (REST API) which works for both published and embedded-published datasources.
    Returns empty dict on failure rather than raising — names are nice-to-have.
    """
    try:
        items = list(TSC.Pager(server.datasources))
        return {ds.id: ds.name for ds in items if ds.id}
    except Exception:
        return {}


# Match: <column ... caption='...' ... name='[Calculation_xxx]' ... > ... <calculation ... formula='...' />
CALC_COLUMN_RE = re.compile(
    r"<column\b(?P<attrs>[^>]*?)>\s*<calculation\b[^>]*?formula=(['\"])(?P<formula>.*?)\2",
    re.DOTALL,
)
CAPTION_RE = re.compile(r"caption=(['\"])([^'\"]+)\1")
NAME_RE = re.compile(r"\bname=(['\"])\[(Calculation_\d+)\]\1")
INTERNAL_REF_RE = re.compile(r"\[Calculation_\d+\]")


def parse_tds_calc_map(xml_text: str) -> dict:
    """
    Extract {internal_name: {caption, formula, raw_formula}} from a Tableau .tds XML string.
    `internal_name` is the bare 'Calculation_<digits>' (no brackets).
    `formula` is the formula with internal Calculation_ references substituted with captions where possible.
    `raw_formula` is the unsubstituted, HTML-decoded formula.
    """
    raw_map = {}
    for match in CALC_COLUMN_RE.finditer(xml_text):
        attrs = match.group("attrs")
        formula = match.group("formula")
        name_m = NAME_RE.search(attrs)
        if not name_m:
            continue
        internal = name_m.group(2)
        caption_m = CAPTION_RE.search(attrs)
        caption = caption_m.group(2) if caption_m else internal
        raw_map[internal] = {
            "caption": html.unescape(caption),
            "raw_formula": html.unescape(formula),
        }

    # Second pass: substitute internal refs with captions for readability
    for internal, info in raw_map.items():
        formula = info["raw_formula"]

        def sub_ref(m):
            ref = m.group(0).strip("[]")
            if ref in raw_map:
                return f"[{raw_map[ref]['caption']}]"
            return m.group(0)

        info["formula"] = INTERNAL_REF_RE.sub(sub_ref, formula)

    return raw_map


def download_datasource_calc_map(server, datasource_id: str) -> dict:
    """
    Download the .tdsx for a datasource, extract the .tds XML, parse calc fields.
    Returns {Calculation_xxx: {caption, formula, raw_formula}} or empty dict on failure.
    """
    tmpdir = os.environ.get("TMPDIR", tempfile.gettempdir())
    tdsx_base = os.path.join(tmpdir, f"pulseaudit_{datasource_id}")
    try:
        server.datasources.download(datasource_id, filepath=tdsx_base, include_extract=False)
    except Exception:
        return {}

    tdsx_path = tdsx_base + ".tdsx"
    if not os.path.exists(tdsx_path):
        # TSC may have written without the suffix
        tdsx_path = tdsx_base if os.path.exists(tdsx_base) else None
    if not tdsx_path:
        return {}

    try:
        with zipfile.ZipFile(tdsx_path) as z:
            tds_name = next((n for n in z.namelist() if n.endswith(".tds")), None)
            if not tds_name:
                return {}
            with z.open(tds_name) as f:
                xml_text = f.read().decode("utf-8", errors="replace")
        return parse_tds_calc_map(xml_text)
    except Exception:
        return {}
    finally:
        try:
            os.remove(tdsx_path)
        except Exception:
            pass


def filter_schema(fields: list) -> list:
    """Remove hidden fields and known Tableau system fields."""
    skip_names = {"Number of Records", "Measure Names", "Measure Values"}
    out = []
    for f in fields:
        name = f.get("name", "")
        if f.get("isHidden"):
            continue
        if name in skip_names:
            continue
        if name.startswith(":"):
            continue
        out.append(f)
    return out


def list_datasources(server: "TSC.Server") -> list:
    """List all datasources on the site, sorted by name."""
    return sorted(list(TSC.Pager(server.datasources)), key=lambda ds: ds.name.lower())
