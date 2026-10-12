#!/usr/bin/env python3
"""
weather_notifier.py
===================
Automated Severe Weather Alert Notification & Station Correlator for the
Oklahoma Seismic Network State of Health (SOH) platform.

Ingests active severe weather alerts directly from the National Weather Service (NWS / NOAA API),
filters strictly for high-impact severe hazards (Tornado, Severe Thunderstorm, Flash Flood,
High Wind, Blizzard, Ice Storm), performs spatial correlation against Oklahoma seismic stations,
and dispatches rich alerts directly through existing communication pipelines:
  1. Slack Incoming Webhooks (Block Kit Cards)
  2. Microsoft Teams Webhooks (Adaptive Cards v1.4)
  3. Email via s-nail (High-Priority plain text briefings)

Features:
  - Strict severe weather filtering (ignores minor advisories and routine statements).
  - Two-tier spatial matching:
      a) High-precision GeoJSON polygon ray-casting for storm cells.
      b) County / UGC zone intersection for regional watches.
  - De-duplicated alerting via persistent state file (.alert_state_weather.json).
  - Cross-references station State of Health from soh_status.json (Online vs Offline).
  - Exports weather_alerts.json for cross-tagging station telemetry drop alerts.
  - Field crew safety advisories warning technicians about hazardous conditions.
  - Supports --dry-run, --test, --status-only, and channel-specific toggles.
"""

import os
import sys
import csv
import json
import argparse
import subprocess
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    requests = None

# Default File Paths
DEFAULT_STATE_FILE = ".alert_state_weather.json"
DEFAULT_META_FILE = "ogs_stations.csv"
DEFAULT_COUNTIES_FILE = "station_counties.json"
DEFAULT_STATUS_FILE = "soh_status.json"
DEFAULT_OUTPUT_FILE = "weather_alerts.json"
DEFAULT_SLACK_WEBHOOK_FILE = "slack_webhook"
DEFAULT_TEAMS_WEBHOOK_FILE = "teams_webhook"
DEFAULT_RECIPIENT = "YOUREMAIL@YOURADDRESS"

# NWS API Configuration
NWS_API_URL = "https://api.weather.gov/alerts/active?area=OK,KS"
NWS_USER_AGENT = "(OklahomaSeismicNetwork_SOH, seismic@ogs.ou.edu)"

# Severe hazard keywords to match against event names
SEVERE_EVENT_KEYWORDS = [
    "Tornado Warning",
    "Tornado Watch",
    "Severe Thunderstorm Warning",
    "Severe Thunderstorm Watch",
    "Flash Flood Warning",
    "Flash Flood Watch",
    "Flood Warning",
    "High Wind Warning",
    "Extreme Wind Warning",
    "High Wind Watch",
    "Blizzard Warning",
    "Winter Storm Warning",
    "Ice Storm Warning",
    "Red Flag Warning",
]

# Explicit exclusions to filter out routine/low-impact notices
EXCLUDED_KEYWORDS = [
    "Air Quality",
    "Heat Advisory",
    "Rip Current",
    "Hydrologic Outlook",
    "Beach Hazards",
    "Special Marine",
    "Small Craft",
    "Frost Advisory",
    "Freeze Watch",
]


# ==============================================================================
# 1. Spatial Intersection & Geometry Helpers
# ==============================================================================

def point_in_polygon(x, y, poly):
    """
    Ray-casting point-in-polygon algorithm in pure Python.
    x = longitude, y = latitude
    poly = [[lon1, lat1], [lon2, lat2], ...]
    """
    n = len(poly)
    if n < 3:
        return False
    inside = False
    p1x, p1y = poly[0]
    for i in range(1, n + 1):
        p2x, p2y = poly[i % n]
        if y > min(p1y, p2y):
            if y <= max(p1y, p2y):
                if x <= max(p1x, p2x):
                    if p1y != p2y:
                        xinters = (y - p1y) * (p2x - p1x) / (p2y - p1y) + p1x
                    if p1x == p2x or x <= xinters:
                        inside = not inside
        p1x, p1y = p2x, p2y
    return inside


def station_in_geometry(lon, lat, geometry):
    """
    Checks if a station coordinate (lon, lat) falls within a GeoJSON geometry
    (Polygon or MultiPolygon).
    """
    if not geometry or not isinstance(geometry, dict):
        return False

    geom_type = geometry.get("type", "")
    coords = geometry.get("coordinates", [])

    if geom_type == "Polygon":
        for ring in coords:
            if point_in_polygon(lon, lat, ring):
                return True
    elif geom_type == "MultiPolygon":
        for poly in coords:
            for ring in poly:
                if point_in_polygon(lon, lat, ring):
                    return True
    return False


# ==============================================================================
# 2. Webhook & Pipeline Resolvers
# ==============================================================================

def resolve_slack_webhook(cli_url=None, webhook_file=DEFAULT_SLACK_WEBHOOK_FILE):
    """Resolves Slack Webhook URL."""
    if cli_url and cli_url.strip():
        return cli_url.strip()
    env_url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if env_url:
        return env_url
    candidates = [
        webhook_file,
        os.path.join(os.path.dirname(os.path.abspath(__file__)), webhook_file)
    ]
    for p in candidates:
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    url = f.read().strip()
                    if url.startswith("https://hooks.slack.com/"):
                        return url
            except Exception:
                pass
    return None


def resolve_teams_webhook(cli_url=None, webhook_file=DEFAULT_TEAMS_WEBHOOK_FILE):
    """Resolves Teams Webhook URL."""
    if cli_url and cli_url.strip():
        return cli_url.strip()
    env_url = os.environ.get("TEAMS_WEBHOOK_URL", "").strip()
    if env_url:
        return env_url
    candidates = [
        webhook_file,
        os.path.join(os.path.dirname(os.path.abspath(__file__)), webhook_file)
    ]
    for p in candidates:
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    url = f.read().strip()
                    if url.startswith("http"):
                        return url
            except Exception:
                pass
    return None


# ==============================================================================
# 3. Data Loaders
# ==============================================================================

def load_stations_and_counties(meta_file=DEFAULT_META_FILE, counties_file=DEFAULT_COUNTIES_FILE):
    """
    Loads station metadata and pre-resolved county information.
    Returns: dict keyed by 'NET.STA' with full metadata and county data.
    """
    stations = {}
    if not os.path.isfile(meta_file):
        return stations

    # Load counties mapping if available
    counties = {}
    if os.path.isfile(counties_file):
        try:
            with open(counties_file, "r", encoding="utf-8") as f:
                counties = json.load(f)
        except Exception as e:
            sys.stderr.write(f"Warning: Failed to load {counties_file}: {e}\n")

    try:
        with open(meta_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                net = row.get("Network", "").strip()
                sta = row.get("Station", "").strip()
                key = f"{net}.{sta}"
                try:
                    lat = float(row.get("Latitude", 0.0))
                    lon = float(row.get("Longitude", 0.0))
                except (ValueError, TypeError):
                    continue

                c_info = counties.get(key, {})
                stations[key] = {
                    "network": net,
                    "station": sta,
                    "latitude": lat,
                    "longitude": lon,
                    "site_name": row.get("SiteName", "Unknown").strip(),
                    "elevation_m": row.get("Elevation_m", "0.0").strip(),
                    "county": c_info.get("county", ""),
                    "fips": c_info.get("fips", ""),
                    "same": c_info.get("same", ""),
                    "ugc": c_info.get("ugc", ""),
                }
    except Exception as e:
        sys.stderr.write(f"Warning: Failed reading {meta_file}: {e}\n")

    return stations


def load_soh_status(status_file=DEFAULT_STATUS_FILE):
    """Loads station State of Health statuses (Online, Delayed, Offline)."""
    status_map = {}
    if not os.path.isfile(status_file):
        return status_map

    try:
        with open(status_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        for item in data.get("inventory", []):
            sta = item.get("station")
            st = item.get("status", "UNKNOWN")
            lat = item.get("latency_s")
            # Prefer higher status if duplicated
            if sta not in status_map or (status_map[sta]["status"] == "OFFLINE" and st in ("ONLINE", "DELAYED")):
                status_map[sta] = {"status": st, "latency_s": lat}
    except Exception:
        pass
    return status_map


def load_alert_state(state_file=DEFAULT_STATE_FILE):
    """Loads state tracking dictionary of alerted NWS alert IDs."""
    if os.path.isfile(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "alerted_ids": {},
        "last_check_ts": None
    }


def save_alert_state(state, state_file=DEFAULT_STATE_FILE):
    """Saves alert state dictionary atomically and prunes expired alerts with timezone awareness."""
    now_dt = datetime.now(timezone.utc)
    now_utc_str = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Prune expired alerts
    active_ids = {}
    for aid, item in state.get("alerted_ids", {}).items():
        exp = item.get("expires")
        if exp:
            try:
                dt_exp = datetime.fromisoformat(exp.replace("Z", "+00:00"))
                # If dt_exp is naive, assume UTC
                if dt_exp.tzinfo is None:
                    dt_exp = dt_exp.replace(tzinfo=timezone.utc)
                if dt_exp > now_dt:
                    active_ids[aid] = item
                    continue
                else:
                    # Expired, skip
                    continue
            except Exception:
                pass
        active_ids[aid] = item  # keep if parse fails

    state["alerted_ids"] = active_ids
    state["last_check_ts"] = now_utc_str

    try:
        tmp_file = f"{state_file}.tmp.{os.getpid()}"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_file, state_file)
    except Exception as e:
        sys.stderr.write(f"Warning: Failed to save alert state atomically: {e}\n")


# ==============================================================================
# 4. NWS Severe Weather Ingestion & Filtering
# ==============================================================================

def is_severe_alert(properties):
    """
    Evaluates whether an NWS alert qualifies strictly as a SEVERE weather alert.
    Criteria:
      1. Severity is 'Extreme' or 'Severe'.
      2. Event matches known severe hazard keywords (Tornado, Severe Thunderstorm,
         Flash Flood, High Wind, Blizzard, etc.).
      3. Not in explicit exclusion list (Air Quality, Heat Advisory, etc.).
    """
    event = (properties.get("event") or "").strip()
    severity = (properties.get("severity") or "").strip()
    status = (properties.get("status") or "").strip()

    # Must be an actual operational alert (not test or draft)
    if status.lower() != "actual":
        return False

    # Check exclusions first
    for excl in EXCLUDED_KEYWORDS:
        if excl.lower() in event.lower():
            return False

    # Check severity level
    if severity in ("Extreme", "Severe"):
        return True

    # Check severe keywords (including Watches)
    for kw in SEVERE_EVENT_KEYWORDS:
        if kw.lower() in event.lower():
            return True

    return False


def fetch_nws_alerts(api_url=NWS_API_URL, user_agent=NWS_USER_AGENT, max_retries=2):
    """Fetches active alerts from NWS API for OK and KS with automatic retry."""
    import time
    headers = {"User-Agent": user_agent, "Accept": "application/geo+json"}
    for attempt in range(max_retries + 1):
        try:
            if requests:
                resp = requests.get(api_url, headers=headers, timeout=12)
                if resp.status_code == 200:
                    return resp.json().get("features", [])
                elif resp.status_code in (500, 502, 503, 504) and attempt < max_retries:
                    time.sleep(1.5)
                    continue
                else:
                    sys.stderr.write(f"Warning: NWS API HTTP {resp.status_code}\n")
                    return []
            else:
                import urllib.request
                req = urllib.request.Request(api_url, headers=headers)
                with urllib.request.urlopen(req, timeout=12) as response:
                    data = json.loads(response.read().decode("utf-8"))
                    return data.get("features", [])
        except Exception as e:
            if attempt < max_retries:
                time.sleep(1.5)
                continue
            sys.stderr.write(f"Warning: Failed to fetch NWS alerts after retries: {e}\n")
            return []
    return []


def correlate_alert_with_stations(alert_feature, stations, soh_map):
    """
    Determines which Oklahoma seismic stations fall within an active alert.
    Uses:
      1. Exact polygon/multipolygon intersection if geometry exists.
      2. County / SAME / UGC / areaDesc match if geometry is null.
    Returns: list of dicts with impacted station details.
    """
    props = alert_feature.get("properties", {}) or {}
    geom = alert_feature.get("geometry")

    area_desc = props.get("areaDesc") or ""
    geocode = props.get("geocode", {}) or {}
    alert_sames = set(geocode.get("SAME") or [])
    alert_ugcs = set(geocode.get("UGC") or [])

    # Split county names from areaDesc
    alert_counties = [c.strip().lower() for c in area_desc.split(";") if c.strip()]

    impacted = []

    for key, sta_info in stations.items():
        sta = sta_info["station"]
        net = sta_info["network"]
        lat = sta_info["latitude"]
        lon = sta_info["longitude"]
        county = sta_info.get("county", "").lower()
        same = sta_info.get("same", "")
        ugc = sta_info.get("ugc", "")

        matched = False

        if geom:
            # 1. Exact Polygon Match
            if station_in_geometry(lon, lat, geom):
                matched = True
        else:
            # 2. County / UGC / areaDesc Match
            if same and same in alert_sames:
                matched = True
            elif ugc and ugc in alert_ugcs:
                matched = True
            elif county and any(county in ac for ac in alert_counties):
                matched = True

        if matched:
            soh = soh_map.get(sta, {"status": "UNKNOWN", "latency_s": None})
            impacted.append({
                "network": net,
                "station": sta,
                "key": key,
                "site_name": sta_info["site_name"],
                "county": sta_info.get("county", "N/A"),
                "latitude": lat,
                "longitude": lon,
                "status": soh["status"],
                "latency_s": soh.get("latency_s")
            })

    return impacted


# ==============================================================================
# 5. Alert Formatting & Dispatch
# ==============================================================================

def format_event_badge(event_name):
    """Assigns appropriate emoji to severe hazard type."""
    ev = event_name.lower()
    if "tornado" in ev:
        return "🌪️"
    elif "thunderstorm" in ev:
        return "⛈️"
    elif "flood" in ev:
        return "🌊"
    elif "wind" in ev:
        return "💨"
    elif "blizzard" in ev or "winter" in ev or "ice" in ev:
        return "❄️"
    elif "fire" in ev or "red flag" in ev:
        return "🔥"
    return "⚠️"


def build_slack_weather_payload(alert, impacted_stations):
    """Builds a rich Slack Block Kit card for a severe weather warning."""
    props = alert.get("properties", {})
    event = props.get("event") or "Severe Weather Alert"
    severity = props.get("severity") or "Severe"
    urgency = props.get("urgency") or "Immediate"
    headline = props.get("headline") or event
    desc = (props.get("description") or "").strip()
    sender = props.get("senderName") or "National Weather Service"
    expires = props.get("expires") or "N/A"

    emoji = format_event_badge(event)
    color = "#E01E5A" if severity == "Extreme" or "Warning" in event else "#ECB22E"

    # Truncate description for readability
    if len(desc) > 350:
        desc = desc[:347] + "..."

    # Impacted stations list
    station_lines = []
    for s in impacted_stations[:15]:
        st_icon = "🟢" if s["status"] == "ONLINE" else "🟡" if s["status"] == "DELAYED" else "🔴"
        station_lines.append(f"• `{s['key']}` ({s['site_name']}): {st_icon} *{s['status']}*")

    if len(impacted_stations) > 15:
        station_lines.append(f"... plus {len(impacted_stations) - 15} more stations")

    stations_text = "\n".join(station_lines) if station_lines else "None detected inside polygon."

    payload = {
        "text": f"{emoji} [NWS SEVERE WEATHER ALERT] {event} affecting {len(impacted_stations)} Oklahoma Seismic Stations",
        "attachments": [
            {
                "color": color,
                "blocks": [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": f"{emoji} NWS Severe Weather Alert: {event}",
                            "emoji": True
                        }
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*{headline}*\n\n{desc}"
                        }
                    },
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": f"*Issuing Office:*\n{sender}"},
                            {"type": "mrkdwn", "text": f"*Severity / Urgency:*\n`{severity}` | `{urgency}`"},
                            {"type": "mrkdwn", "text": f"*Valid Until:*\n{expires}"},
                            {"type": "mrkdwn", "text": f"*Impacted Stations:*\n*{len(impacted_stations)} stations*"}
                        ]
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*Affected Oklahoma Seismic Stations:*\n{stations_text}"
                        }
                    },
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": "⚠️ *Field Safety Advisory:* Severe weather hazards in effect. Site access hazardous; suspend field maintenance dispatches to impacted stations until warning expires."
                            }
                        ]
                    }
                ]
            }
        ]
    }
    return payload


def build_teams_weather_card(alert, impacted_stations):
    """Builds a rich Adaptive Card v1.4 for Microsoft Teams."""
    props = alert.get("properties", {})
    event = props.get("event") or "Severe Weather Alert"
    severity = props.get("severity") or "Severe"
    urgency = props.get("urgency") or "Immediate"
    headline = props.get("headline") or event
    desc = (props.get("description") or "").strip()
    sender = props.get("senderName") or "National Weather Service"
    expires = props.get("expires") or "N/A"

    emoji = format_event_badge(event)
    card_style = "attention" if severity == "Extreme" or "Warning" in event else "warning"

    if len(desc) > 350:
        desc = desc[:347] + "..."

    station_facts = []
    for s in impacted_stations[:12]:
        st_icon = "🟢" if s["status"] == "ONLINE" else "🟡" if s["status"] == "DELAYED" else "🔴"
        station_facts.append({
            "title": s["key"],
            "value": f"{st_icon} {s['status']} ({s['site_name']})"
        })

    if not station_facts:
        station_facts.append({
            "title": "Stations",
            "value": "None detected in direct polygon/county path"
        })
    elif len(impacted_stations) > 12:
        station_facts.append({
            "title": "More Stations",
            "value": f"+ {len(impacted_stations) - 12} additional stations"
        })

    card_content = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "Container",
                "style": card_style,
                "bleed": True,
                "items": [
                    {
                        "type": "TextBlock",
                        "text": f"{emoji} NWS Severe Weather Alert: {event}",
                        "weight": "Bolder",
                        "size": "Large",
                        "color": "Attention"
                    },
                    {
                        "type": "TextBlock",
                        "text": f"Issued by {sender} | Valid Until: {expires}",
                        "isSubtle": True,
                        "spacing": "None"
                    }
                ]
            },
            {
                "type": "Container",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": headline,
                        "weight": "Bolder",
                        "wrap": True
                    },
                    {
                        "type": "TextBlock",
                        "text": desc,
                        "wrap": True,
                        "isSubtle": True
                    }
                ]
            },
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Hazard Severity", "value": f"{severity} ({urgency})"},
                    {"title": "Impacted Stations", "value": f"{len(impacted_stations)} stations"},
                    {"title": "Expiration", "value": expires}
                ]
            },
            {
                "type": "TextBlock",
                "text": f"Impacted Seismic Stations ({len(impacted_stations)}):",
                "weight": "Bolder",
                "spacing": "Medium"
            },
            {
                "type": "FactSet",
                "facts": station_facts
            },
            {
                "type": "Container",
                "style": "emphasis",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": "⚠️ Field Safety Advisory: Severe environmental hazards active. Avoid dispatching field personnel to impacted station sites until warning expires.",
                        "wrap": True,
                        "weight": "Bolder",
                        "color": "Warning"
                    }
                ]
            }
        ]
    }

    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": card_content
            }
        ]
    }


def build_email_weather_body(alert, impacted_stations):
    """Builds a plain-text email briefing for s-nail dispatch."""
    props = alert.get("properties", {})
    event = props.get("event") or "Severe Weather Alert"
    severity = props.get("severity") or "Severe"
    urgency = props.get("urgency") or "Immediate"
    headline = props.get("headline") or event
    desc = (props.get("description") or "").strip()
    sender = props.get("senderName") or "National Weather Service"
    expires = props.get("expires") or "N/A"

    emoji = format_event_badge(event)
    subject = f"{emoji} [WEATHER ALERT] {event} Impacting {len(impacted_stations)} Oklahoma Stations"

    station_lines = []
    for s in impacted_stations:
        st_icon = "🟢 ONLINE" if s["status"] == "ONLINE" else "🟡 DELAYED" if s["status"] == "DELAYED" else "🔴 OFFLINE"
        station_lines.append(f"  • {s['key']:<10} : {s['site_name']:<28} ({s['county']} Co.) | {st_icon}")

    stations_block = "\n".join(station_lines) if station_lines else "  None detected inside polygon."

    body = f"""======================================================================
  {emoji} OKLAHOMA SEISMIC NETWORK SEVERE WEATHER HAZARD ALERT
  National Weather Service Alert Dispatch
======================================================================

EVENT TYPE      : {event}
SEVERITY / URGENCY: {severity} / {urgency}
ISSUING OFFICE  : {sender}
VALID UNTIL     : {expires}

HEADLINE:
{headline}

HAZARD SUMMARY:
{desc}

----------------------------------------------------------------------
IMPACTED OKLAHOMA SEISMIC STATIONS ({len(impacted_stations)})
----------------------------------------------------------------------
{stations_block}

----------------------------------------------------------------------
FIELD OPERATIONS SAFETY ADVISORY
----------------------------------------------------------------------
⚠️ Severe weather conditions are in effect in the vicinity of the above
seismic stations. Dirt access roads, flash flooding, and lightning risk
present extreme hazards to personnel.

RECOMMENDED ACTION:
1. Defer all scheduled field maintenance dispatches to these stations.
2. Note that telemetry dropouts or latency spikes during this window
   are likely caused by local storm effects, lightning, or commercial
   power/cell tower disruptions.
3. Monitor station telemetry recovery once the storm cell clears.

======================================================================
This is an automated alert from the Oklahoma Seismic Network
State of Health Monitoring Platform.
======================================================================
"""
    return subject, body


def post_webhook(url, payload, label="Webhook", dry_run=False, max_retries=2):
    """Posts JSON payload to webhook URL with automatic retries on transient errors."""
    if dry_run:
        print(f"\n================ [DRY-RUN {label.upper()}] ================")
        print(json.dumps(payload, indent=2))
        print("=========================================================\n")
        return True

    if not url:
        return False

    import time
    for attempt in range(max_retries + 1):
        try:
            data = json.dumps(payload).encode("utf-8")
            if requests:
                resp = requests.post(url, json=payload, timeout=15)
                # Accept all HTTP 2xx success codes (200 OK, 201 Created, 202 Accepted, 204 No Content).
                # Microsoft Teams / Power Automate Workflows standardly return HTTP 202 Accepted.
                if 200 <= resp.status_code < 300:
                    return True
                elif resp.status_code in (500, 502, 503, 504) and attempt < max_retries:
                    time.sleep(1.5)
                    continue
                else:
                    sys.stderr.write(f"❌ Error: {label} returned HTTP {resp.status_code}: {resp.text}\n")
                    return False
            else:
                import urllib.request
                import urllib.error
                req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return 200 <= resp.status < 300
        except Exception as e:
            # Check for urllib HTTPError specifically if encountered
            if hasattr(e, "code") and hasattr(e, "read"):
                if e.code in (500, 502, 503, 504) and attempt < max_retries:
                    time.sleep(1.5)
                    continue
                try:
                    err_msg = e.read().decode("utf-8", errors="replace")
                    sys.stderr.write(f"❌ Error: {label} returned HTTP {e.code}: {err_msg}\n")
                    return False
                except Exception:
                    pass
            elif attempt < max_retries:
                time.sleep(1.5)
                continue
            sys.stderr.write(f"❌ Error: Failed posting to {label}: {e}\n")
            return False
    return False


def send_email_via_snail(recipient, subject, body, dry_run=False):
    """Sends email notification using s-nail CLI."""
    if dry_run:
        print("\n================ [DRY-RUN EMAIL NOTIFICATION] ================")
        print(f"To: {recipient}")
        print(f"Subject: {subject}\n")
        print(body)
        print("==============================================================\n")
        return True

    try:
        proc = subprocess.Popen(
            ["s-nail", "-s", subject, recipient],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        stdout, stderr = proc.communicate(input=body)
        if proc.returncode == 0:
            return True
        else:
            sys.stderr.write(f"❌ Error: s-nail failed (code {proc.returncode}): {stderr.strip()}\n")
            return False
    except FileNotFoundError:
        sys.stderr.write("❌ Error: s-nail binary not found in PATH.\n")
        return False
    except Exception as e:
        sys.stderr.write(f"❌ Error: Failed to execute s-nail: {e}\n")
        return False


# ==============================================================================
# 6. Main Orchestrator
# ==============================================================================

def run_weather_check(args):
    """Executes full severe weather check, station correlation, and alerting."""
    # 1. Load Station Metadata and Counties
    stations = load_stations_and_counties(args.csv, args.counties)
    if not stations:
        sys.stderr.write(f"Error: No stations loaded from {args.csv}\n")
        return 1

    # 2. Load latest SOH Status
    soh_map = load_soh_status(args.status_file)

    # 3. Load Alert State
    state = load_alert_state(args.state_file)
    alerted_ids = state.get("alerted_ids", {})

    # 4. Resolve Webhooks
    slack_url = resolve_slack_webhook(args.slack_webhook)
    teams_url = resolve_teams_webhook(args.teams_webhook)
    email_recipient = args.recipient

    # Test Mode Handler
    if args.test:
        print("🧪 [TEST MODE] Dispatching mock severe weather alert...")
        mock_alert = {
            "id": "urn:oid:test.severe.weather.001",
            "properties": {
                "event": "Severe Thunderstorm Warning",
                "severity": "Severe",
                "urgency": "Immediate",
                "headline": "TEST: Severe Thunderstorm Warning issued for Canadian & Oklahoma Counties",
                "description": "At 11:15 AM CDT, severe thunderstorms capable of producing 65 mph wind gusts and quarter size hail were located near El Reno, moving east at 40 mph.",
                "senderName": "NWS Norman OK",
                "expires": datetime.now(timezone.utc).isoformat(),
                "areaDesc": "Canadian; Oklahoma"
            }
        }
        mock_impacted = [
            {"network": "OK", "station": "CHOK", "key": "OK.CHOK", "site_name": "Choctaw", "county": "Oklahoma", "latitude": 35.5, "longitude": -97.3, "status": "ONLINE", "latency_s": 2.1},
            {"network": "O2", "station": "ERNS", "key": "O2.ERNS", "site_name": "El Reno", "county": "Canadian", "latitude": 35.4, "longitude": -98.0, "status": "OFFLINE", "latency_s": None},
        ]

        if not args.no_slack and slack_url:
            payload = build_slack_weather_payload(mock_alert, mock_impacted)
            ok = post_webhook(slack_url, payload, "Slack", args.dry_run)
            print("  Slack test dispatch:", "✅ Sent" if ok else "❌ Failed")

        if not args.no_teams and teams_url:
            payload = build_teams_weather_card(mock_alert, mock_impacted)
            ok = post_webhook(teams_url, payload, "Teams", args.dry_run)
            print("  Teams test dispatch:", "✅ Sent" if ok else "❌ Failed")

        if not args.no_email and email_recipient:
            subj, body = build_email_weather_body(mock_alert, mock_impacted)
            ok = send_email_via_snail(email_recipient, subj, body, args.dry_run)
            print("  Email test dispatch:", "✅ Sent" if ok else "❌ Failed")

        return 0

    # 5. Fetch Active Alerts from NWS
    print("📡 Polling National Weather Service API for active severe weather alerts (OK, KS)...")
    raw_alerts = fetch_nws_alerts()
    print(f"   Received {len(raw_alerts)} total active NWS notices.")

    # 6. Filter strictly for Severe Weather
    severe_alerts = []
    for feat in raw_alerts:
        props = feat.get("properties", {})
        if is_severe_alert(props):
            severe_alerts.append(feat)

    print(f"   Filtered to {len(severe_alerts)} active SEVERE weather alerts.")

    # 7. Correlate with Stations
    active_incidents = []
    all_impacted_stations = {}

    for alert in severe_alerts:
        aid = alert.get("id", alert.get("properties", {}).get("id"))
        props = alert.get("properties", {})
        event = props.get("event", "Unknown Hazard")
        expires = props.get("expires", "")

        impacted = correlate_alert_with_stations(alert, stations, soh_map)
        if not impacted:
            continue

        active_incidents.append({
            "alert": alert,
            "impacted": impacted,
            "id": aid
        })

        for imp in impacted:
            all_impacted_stations[imp["key"]] = {
                "station": imp["station"],
                "network": imp["network"],
                "event": event,
                "severity": props.get("severity", "Severe"),
                "office": props.get("senderName", "NWS"),
                "headline": props.get("headline", ""),
                "expires": expires
            }
            # Also key by station code
            all_impacted_stations[imp["station"]] = all_impacted_stations[imp["key"]]

    # 8. Export Snapshot to weather_alerts.json for downstream drop cross-tagging
    snapshot_data = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "active_severe_alerts_count": len(active_incidents),
        "impacted_stations_count": len(all_impacted_stations) // 2 if all_impacted_stations else 0,
        "impacted_stations": all_impacted_stations,
        "incidents": [
            {
                "id": inc["id"],
                "event": inc["alert"]["properties"].get("event"),
                "severity": inc["alert"]["properties"].get("severity"),
                "expires": inc["alert"]["properties"].get("expires"),
                "impacted_count": len(inc["impacted"]),
                "impacted_stations": [s["key"] for s in inc["impacted"]]
            }
            for inc in active_incidents
        ]
    }
    try:
        tmp_out = f"{args.output}.tmp.{os.getpid()}"
        with open(tmp_out, "w", encoding="utf-8") as f:
            json.dump(snapshot_data, f, indent=2)
        os.replace(tmp_out, args.output)
        print(f"💾 Active weather hazard snapshot exported: {args.output}")
    except Exception as e:
        sys.stderr.write(f"Warning: Failed to write {args.output} atomically: {e}\n")

    # Status-only summary output
    if args.status_only:
        print("\n================ ACTIVE SEVERE WEATHER HAZARDS ================")
        if not active_incidents:
            print("✅ No active severe weather warnings currently impacting Oklahoma seismic stations.")
        else:
            for inc in active_incidents:
                p = inc["alert"]["properties"]
                print(f"🚨 {p.get('event')} ({p.get('severity')}) - Valid until: {p.get('expires')}")
                print(f"   Headline: {p.get('headline')}")
                print(f"   Impacted Stations ({len(inc['impacted'])}): {', '.join(s['key'] for s in inc['impacted'][:10])}")
        print("===============================================================\n")
        return 0

    # 9. Evaluate New vs Already-Alerted Incidents
    new_incidents = []
    for inc in active_incidents:
        aid = inc["id"]
        if aid in alerted_ids:
            continue
        new_incidents.append(inc)

    if not new_incidents:
        if active_incidents:
            print(f"ℹ️  {len(active_incidents)} severe weather incident(s) active, but all have already been alerted.")
        else:
            print("✅ No active severe weather warnings impacting Oklahoma seismic network.")
        save_alert_state(state, args.state_file)
        return 0

    print(f"🚨 Found {len(new_incidents)} NEW severe weather alert(s) impacting seismic stations!")

    # 10. Dispatch New Alerts Across Configured Channels
    for inc in new_incidents:
        alert = inc["alert"]
        impacted = inc["impacted"]
        aid = inc["id"]
        props = alert.get("properties", {})
        event = props.get("event")

        print(f"\n📤 Dispatching alerts for: {event} ({len(impacted)} impacted stations)...")

        any_success = False

        # A. Slack
        if not args.no_slack:
            if slack_url:
                payload = build_slack_weather_payload(alert, impacted)
                ok = post_webhook(slack_url, payload, "Slack", args.dry_run)
                print("   Slack Notification:", "✅ Posted" if ok else "❌ Failed")
                if ok:
                    any_success = True
            else:
                print("   Slack Notification: ⚠️ Skipped (no webhook URL found)")

        # B. Microsoft Teams
        if not args.no_teams:
            if teams_url:
                payload = build_teams_weather_card(alert, impacted)
                ok = post_webhook(teams_url, payload, "Teams", args.dry_run)
                print("   Teams Notification:", "✅ Posted" if ok else "❌ Failed")
                if ok:
                    any_success = True
            else:
                print("   Teams Notification: ⚠️ Skipped (no webhook URL found)")

        # C. Email via s-nail
        if not args.no_email:
            if email_recipient:
                subj, body = build_email_weather_body(alert, impacted)
                ok = send_email_via_snail(email_recipient, subj, body, args.dry_run)
                print("   Email Notification:", "✅ Sent" if ok else "❌ Failed")
                if ok:
                    any_success = True
            else:
                print("   Email Notification: ⚠️ Skipped (no recipient configured)")

        # Record in state only if at least one channel was successfully notified (or dry-run)
        if any_success or args.dry_run:
            alerted_ids[aid] = {
                "event": event,
                "severity": props.get("severity"),
                "alerted_at": datetime.now(timezone.utc).isoformat(),
                "expires": props.get("expires"),
                "impacted_count": len(impacted),
                "impacted_stations": [s["key"] for s in impacted]
            }
        else:
            print(f"   ⚠️ Alert dispatch failed across all channels for {aid}. Will retry on next check.")

    # Save state
    state["alerted_ids"] = alerted_ids
    save_alert_state(state, args.state_file)
    print("\n✅ Severe weather check and dispatch completed.")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Oklahoma Seismic Network Severe Weather Alert Notifier & Station Correlator"
    )
    parser.add_argument("--csv", default=DEFAULT_META_FILE, help="Path to station inventory CSV")
    parser.add_argument("--counties", default=DEFAULT_COUNTIES_FILE, help="Path to station counties JSON")
    parser.add_argument("--status-file", default=DEFAULT_STATUS_FILE, help="Path to SOH status JSON")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_FILE, help="Path to export weather_alerts.json")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE, help="Path to persistent alert state file")

    parser.add_argument("--slack-webhook", help="Slack Incoming Webhook URL")
    parser.add_argument("--teams-webhook", help="Microsoft Teams Webhook URL")
    parser.add_argument("--recipient", default=DEFAULT_RECIPIENT, help="Email recipient address for s-nail")

    parser.add_argument("--no-slack", action="store_true", help="Disable Slack notifications")
    parser.add_argument("--no-teams", action="store_true", help="Disable Teams notifications")
    parser.add_argument("--no-email", action="store_true", help="Disable Email notifications")

    parser.add_argument("--dry-run", action="store_true", help="Print payloads without posting to network/email")
    parser.add_argument("--test", action="store_true", help="Dispatch a test mock severe weather alert card")
    parser.add_argument("--status-only", action="store_true", help="Print active severe weather hazards and exit")

    args = parser.parse_args()
    sys.exit(run_weather_check(args))


if __name__ == "__main__":
    main()
