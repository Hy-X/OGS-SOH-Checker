#!/usr/bin/env python3
"""
alert_notifier_slack.py
=======================
Automated State of Health (SOH) alert notification script for the Oklahoma Seismic Network
using Slack Incoming Webhooks.

Detects when a seismic station drops (Online -> Offline) or recovers (Offline -> Online)
and sends rich, formatted Slack message cards to a specified Slack channel.

Features:
  - Deduplicated alerting via persistent state file (.alert_state_slack.json) to prevent notification spam.
  - Automatically suppresses alerts for chronic/baseline offline stations on initial run.
  - Formats detailed Slack Block Kit cards with station metadata, coordinates, network health KPIs, and timestamps.
  - Flexible Webhook resolution: CLI argument, environment variable (SLACK_WEBHOOK_URL), or local file (slack_webhook).
  - Supports --test to verify webhook delivery immediately.
  - Supports --dry-run for testing logic and inspecting payloads without posting to Slack.
  - Supports --status to view active outages.
"""

import os
import sys
import csv
import json
import argparse
import urllib.request
import urllib.error
from datetime import datetime, timezone


DEFAULT_STATE_FILE = ".alert_state_slack.json"
DEFAULT_META_FILE = "ogs_stations.csv"
DEFAULT_STATUS_FILE = "soh_status.json"
DEFAULT_LOG_FILE = "monitor_log.csv"
DEFAULT_WEATHER_FILE = "weather_alerts.json"
DEFAULT_WEBHOOK_FILE = "slack_webhook"
DEFAULT_MASS_DROP_THRESHOLD = 10


def load_weather_hazards(weather_file=DEFAULT_WEATHER_FILE):
    """Loads active severe weather hazard mapping from weather_alerts.json if present."""
    if os.path.isfile(weather_file):
        try:
            with open(weather_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get("impacted_stations", {})
        except Exception:
            pass
    return {}


def resolve_webhook_url(cli_url=None, webhook_file=DEFAULT_WEBHOOK_FILE):
    """
    Resolves Slack Webhook URL in order of priority:
      1. Explicit CLI argument (--webhook-url)
      2. Environment variable SLACK_WEBHOOK_URL
      3. Local text file (default: slack_webhook)
    """
    if cli_url and cli_url.strip():
        return cli_url.strip()

    env_url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if env_url:
        return env_url

    # Check local webhook file
    candidates = [
        webhook_file,
        os.path.join(os.path.dirname(os.path.abspath(__file__)), webhook_file)
    ]
    for p in candidates:
        if os.path.isfile(p):
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    url = f.read().strip()
                    if url.startswith("https://hooks.slack.com/"):
                        return url
            except Exception:
                pass

    return None


def load_station_metadata(meta_file=DEFAULT_META_FILE):
    """Loads station site metadata and coordinates from CSV."""
    meta = {}
    if not os.path.isfile(meta_file):
        return meta

    try:
        with open(meta_file, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                net = row.get("Network", "").strip()
                sta = row.get("Station", "").strip()
                key = f"{net}.{sta}"
                meta[key] = {
                    "network": net,
                    "station": sta,
                    "site_name": row.get("SiteName", "Unknown").strip(),
                    "latitude": row.get("Latitude", "").strip(),
                    "longitude": row.get("Longitude", "").strip(),
                    "elevation_m": row.get("Elevation_m", "").strip(),
                }
                if sta not in meta:
                    meta[sta] = meta[key]
    except Exception as e:
        sys.stderr.write(f"Warning: Failed reading metadata file {meta_file}: {e}\n")
    return meta


def load_latest_telemetry(status_file=DEFAULT_STATUS_FILE, log_file=DEFAULT_LOG_FILE):
    """
    Loads latest station status. Prefers soh_status.json, falls back to monitor_log.csv.
    Returns:
        tuple: (timestamp_str, station_status_dict, availability_pct, counts_dict)
    """
    # 1. Try soh_status.json first
    if os.path.isfile(status_file):
        try:
            with open(status_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            ts = data.get("timestamp", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            avail = data.get("availability_pct", 0.0)
            counts = data.get("counts", {})

            station_status = {}
            for item in data.get("inventory", []):
                sta = item["station"]
                net = item["network"]
                st = item["status"]
                lat = item.get("latency_s")
                # Deduplicate: prioritize ONLINE > DELAYED > OFFLINE
                if sta in station_status:
                    curr_st = station_status[sta]["status"]
                    if curr_st == "OFFLINE" and st in ("ONLINE", "DELAYED"):
                        station_status[sta] = {"network": net, "station": sta, "status": st, "latency": lat}
                else:
                    station_status[sta] = {"network": net, "station": sta, "status": st, "latency": lat}

            if station_status:
                return ts, station_status, avail, counts
        except Exception as e:
            sys.stderr.write(f"Warning: Failed to load {status_file} ({e}), checking {log_file}...\n")

    # 2. Fall back to monitor_log.csv
    if os.path.isfile(log_file):
        records = []
        with open(log_file, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                records.append(row)

        if records:
            latest_ts = records[-1]["Timestamp"]
            station_status = {}
            for r in reversed(records):
                if r["Timestamp"] != latest_ts:
                    break
                sta = r["Station"]
                net = r["Network"]
                st = r["Status"]
                lat = float(r["Latency_s"]) if r["Latency_s"] else None
                if sta in station_status:
                    curr_st = station_status[sta]["status"]
                    if curr_st == "OFFLINE" and st in ("ONLINE", "DELAYED"):
                        station_status[sta] = {"network": net, "station": sta, "status": st, "latency": lat}
                else:
                    station_status[sta] = {"network": net, "station": sta, "status": st, "latency": lat}

            total = len(station_status)
            online = sum(1 for s in station_status.values() if s["status"] == "ONLINE")
            delayed = sum(1 for s in station_status.values() if s["status"] == "DELAYED")
            offline = sum(1 for s in station_status.values() if s["status"] == "OFFLINE")
            avail = round((online + delayed) / total * 100, 1) if total > 0 else 0.0
            counts = {"total": total, "online": online, "delayed": delayed, "offline": offline}
            return latest_ts, station_status, avail, counts

    return None, {}, 0.0, {}


def load_alert_state(state_file=DEFAULT_STATE_FILE):
    """Loads alert state dictionary tracking active alerts."""
    if os.path.isfile(state_file):
        try:
            with open(state_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "active_outages": {},    # sta -> {"network": net, "dropped_at": ts, "alerted_at": ts}
        "last_audit_ts": None,
        "initialized": False
    }


def save_alert_state(state, state_file=DEFAULT_STATE_FILE):
    """Saves alert state dictionary atomically."""
    try:
        tmp_file = f"{state_file}.tmp.{os.getpid()}"
        with open(tmp_file, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_file, state_file)
    except Exception as e:
        sys.stderr.write(f"Warning: Failed to save alert state atomically to {state_file}: {e}\n")


def post_to_slack(webhook_url, payload, dry_run=False, max_retries=2):
    """Posts a JSON payload to the Slack webhook URL with automatic retries."""
    if dry_run:
        print("\n================ [DRY-RUN SLACK PAYLOAD] ================")
        print(json.dumps(payload, indent=2))
        print("=========================================================\n")
        return True

    import time
    for attempt in range(max_retries + 1):
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                webhook_url,
                data=data,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                # Accept all HTTP 2xx success codes (200 OK, 201 Created, 202 Accepted, 204 No Content).
                return 200 <= resp.status < 300
        except urllib.error.HTTPError as e:
            if e.code in (500, 502, 503, 504) and attempt < max_retries:
                time.sleep(1.5)
                continue
            err_msg = e.read().decode("utf-8", errors="replace")
            sys.stderr.write(f"❌ Error: Slack Webhook HTTP {e.code}: {err_msg}\n")
            return False
        except Exception as e:
            if attempt < max_retries:
                time.sleep(1.5)
                continue
            sys.stderr.write(f"❌ Error: Failed to post to Slack: {e}\n")
            return False
    return False


def build_slack_drop_card(net, sta, meta, dropped_at, avail_pct, counts, weather_info=None):
    """Builds a rich Slack Block Kit payload for a station drop alert."""
    site_name = meta.get("site_name", "Unknown Location")
    lat = meta.get("latitude", "N/A")
    lon = meta.get("longitude", "N/A")
    elev = meta.get("elevation_m", "N/A")

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🚨 SOH Alert: Station Down: {net}.{sta}",
                "emoji": True
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Station:* `{net}.{sta}`\n*Location:* {site_name}\n*Coordinates:* {lat}, {lon} (Elev: {elev} m)"
            }
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": "*Current Status:*\n🔴 *OFFLINE* (Silent)"},
                {"type": "mrkdwn", "text": f"*Detected At:*\n{dropped_at} UTC"},
                {"type": "mrkdwn", "text": f"*Network Availability:*\n*{avail_pct}%*"},
                {"type": "mrkdwn", "text": f"*Array Health:*\n🟢 {counts.get('online', 0)} | 🟡 {counts.get('delayed', 0)} | 🔴 {counts.get('offline', 0)}"}
            ]
        }
    ]

    if weather_info:
        ev = weather_info.get("event", "Severe Weather Warning")
        off = weather_info.get("office", "NWS")
        exp = weather_info.get("expires", "N/A")
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"⛈️ *Severe Weather Alert Correlation:*\nStation is currently located inside an active *{ev}* zone ({off}, valid until {exp}). Telemetry drop is likely storm-, lightning-, or power grid-related."
            }
        })

    blocks.append({
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": "🔧 *Field Check:* Inspect solar/battery charge controller, cellular modem link, and digitizer power." if not weather_info else "⚠️ *Safety Advisory:* Active severe weather hazard in effect. Defer technician field visits until storm warning clears."
            }
        ]
    })

    payload = {
        "text": f"🚨 [SOH ALERT] Seismic Station Down: {net}.{sta} ({site_name})",
        "attachments": [
            {
                "color": "#E01E5A",  # Red
                "blocks": blocks
            }
        ]
    }
    return payload


def build_slack_mass_drop_card(drops, dropped_at, avail_pct, counts):
    """Builds a rich Slack Block Kit payload for an upstream / correlated mass drop alert."""
    count = len(drops)
    station_codes = [f"{net}.{sta}" for net, sta, meta, info in drops]
    stations_preview = ", ".join(f"`{sc}`" for sc in station_codes[:30])
    if count > 30:
        stations_preview += f" ... plus {count - 30} more"

    payload = {
        "text": f"🚨 [CRITICAL SOH ALERT] Upstream Outage: {count} Stations Dropped Simultaneously",
        "attachments": [
            {
                "color": "#7B0000",  # Dark Crimson / Critical
                "blocks": [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": f"🚨 UPSTREAM OUTAGE: {count} Stations Dropped Simultaneously",
                            "emoji": True
                        }
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f"*⚠️ Correlated Failure Alarm: {count} stations went offline in the exact same audit cycle ({dropped_at} UTC).*\n"
                                "A simultaneous drop of this magnitude indicates an *upstream infrastructure disruption* "
                                "(EarthScope FDSNWS / SeedLink data center, regional cellular carrier backbone, or monitoring host network loss) "
                                "rather than isolated hardware failures at individual field sites."
                            )
                        }
                    },
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": f"*Dropped Stations:*\n🚨 *{count} stations*"},
                            {"type": "mrkdwn", "text": f"*Network Availability:*\n📉 *{avail_pct}%*"},
                            {"type": "mrkdwn", "text": f"*Array Health:*\n🟢 {counts.get('online', 0)} | 🟡 {counts.get('delayed', 0)} | 🔴 {counts.get('offline', 0)}"},
                            {"type": "mrkdwn", "text": f"*Audit Timestamp:*\n{dropped_at} UTC"}
                        ]
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*Affected Stations ({count}):*\n{stations_preview}"
                        }
                    },
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": "🔍 *Upstream Action Checklist:* 1. Check EarthScope DMC services (`service.iris.edu / rtserve.earthscope.org`). 2. Verify cellular carrier backbone. 3. Check monitoring server gateway routing."
                            }
                        ]
                    }
                ]
            }
        ]
    }
    return payload


def build_slack_mass_recovery_card(recoveries, restored_at, avail_pct, counts):
    """Builds a rich Slack Block Kit payload for an upstream mass recovery notice."""
    count = len(recoveries)
    station_codes = [f"{out_info.get('network', 'OK')}.{sta}" for sta, out_info in recoveries]
    stations_preview = ", ".join(f"`{sc}`" for sc in station_codes[:30])
    if count > 30:
        stations_preview += f" ... plus {count - 30} more"

    payload = {
        "text": f"🟢 [SOH RECOVERED] Upstream Connection Restored: {count} Stations Reconnected",
        "attachments": [
            {
                "color": "#166534",  # Deep Green
                "blocks": [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": f"🟢 UPSTREAM RESTORED: {count} Stations Reconnected",
                            "emoji": True
                        }
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f"*Telemetry streaming has resumed across {count} stations simultaneously ({restored_at} UTC).*\n"
                                "Upstream data flow from EarthScope / regional cellular carriers is back to normal operational status."
                            )
                        }
                    },
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": f"*Restored Stations:*\n🟢 *{count} stations*"},
                            {"type": "mrkdwn", "text": f"*Network Availability:*\n📈 *{avail_pct}%*"},
                            {"type": "mrkdwn", "text": f"*Array Health:*\n🟢 {counts.get('online', 0)} | 🟡 {counts.get('delayed', 0)} | 🔴 {counts.get('offline', 0)}"},
                            {"type": "mrkdwn", "text": f"*Restored At:*\n{restored_at} UTC"}
                        ]
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*Restored Stations ({count}):*\n{stations_preview}"
                        }
                    }
                ]
            }
        ]
    }
    return payload


def build_slack_recovery_card(net, sta, meta, restored_at, down_duration_str):
    """Builds a rich Slack Block Kit payload for a station recovery notification."""
    site_name = meta.get("site_name", "Unknown Location")

    payload = {
        "text": f"🟢 [SOH RECOVERED] Station Restored: {net}.{sta} ({site_name})",
        "attachments": [
            {
                "color": "#2EB886",  # Green
                "blocks": [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": f"🟢 SOH Recovery: Station Restored: {net}.{sta}",
                            "emoji": True
                        }
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*Station:* `{net}.{sta}`\n*Location:* {site_name}\nTelemetry packets are once again arriving within normal latency limits."
                        }
                    },
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": "*Current Status:*\n🟢 *ONLINE* (Active)"},
                            {"type": "mrkdwn", "text": f"*Restored At:*\n{restored_at} UTC"},
                            {"type": "mrkdwn", "text": f"*Outage Duration:*\n~{down_duration_str}"},
                            {"type": "mrkdwn", "text": "*Action:*\nNo further field action required"}
                        ]
                    },
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": "Oklahoma Seismic Network SOH Monitor | Automated Telemetry Recovery"
                            }
                        ]
                    }
                ]
            }
        ]
    }
    return payload


def build_slack_test_card():
    """Builds a test message card confirming Slack integration."""
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    payload = {
        "text": "📡 [SOH Test] Slack Webhook Alert Integration Verified",
        "attachments": [
            {
                "color": "#36A64F",
                "blocks": [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": "📡 Oklahoma Seismic Network SOH Monitor",
                            "emoji": True
                        }
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "*Slack Webhook Alerting Test: SUCCESS* :white_check_mark:\nAutomated station drop and recovery notifications are properly configured to broadcast to this channel."
                        }
                    },
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": "*System Status:*\n🟢 Operational"},
                            {"type": "mrkdwn", "text": f"*Test Timestamp:*\n{now_utc}"},
                            {"type": "mrkdwn", "text": "*Target Networks:*\n`OK` & `O2` (Oklahoma)"},
                            {"type": "mrkdwn", "text": "*Delivery Method:*\nSlack Incoming Webhook"}
                        ]
                    }
                ]
            }
        ]
    }
    return payload


def check_and_alert(webhook_url, state_file=DEFAULT_STATE_FILE, dry_run=False,
                    notify_recoveries=True, force=False,
                    mass_drop_threshold=DEFAULT_MASS_DROP_THRESHOLD):
    """
    Main evaluation loop:
      - Correlated / Mass Drop Alarm: Triggers Upstream Outage Alert when > mass_drop_threshold stations drop simultaneously.
      - Normal Drop Alerts: Individual station drop cards when <= mass_drop_threshold.
      - Detects RECOVERIES (Offline -> Online) with mass recovery grouping.
      - Updates alert state tracking file.
    """
    metadata = load_station_metadata()
    latest_ts, current_status, avail_pct, counts = load_latest_telemetry()

    if not current_status:
        sys.stderr.write("Notice: No telemetry data available to evaluate alerts.\n")
        return 0

    state = load_alert_state(state_file)
    active_outages = state.setdefault("active_outages", {})
    is_initial_run = not state.get("initialized", False)

    # First run: seed baseline outages so we don't spam for chronic offline stations
    if is_initial_run and not force:
        print("ℹ️  Initial run: Seeding baseline offline stations into state (suppressing chronic alerts)...")
        for sta, info in current_status.items():
            if info["status"] == "OFFLINE":
                active_outages[sta] = {
                    "network": info["network"],
                    "dropped_at": latest_ts,
                    "alerted_at": latest_ts,
                    "is_baseline": True
                }
        state["initialized"] = True
        state["last_audit_ts"] = latest_ts
        save_alert_state(state, state_file)
        print(f"✅ Baseline established. Tracking {len(active_outages)} pre-existing offline stations.")
        return 0

    alerts_sent = 0

    # 1. Detect NEW DROPS
    new_drops = []
    for sta, info in current_status.items():
        st = info["status"]
        net = info["network"]
        meta = metadata.get(f"{net}.{sta}", metadata.get(sta, {}))

        if st == "OFFLINE":
            should_alert = (sta not in active_outages) or force
            if should_alert:
                new_drops.append((net, sta, meta, info))

    if len(new_drops) > mass_drop_threshold:
        # CORRELATED / MASS DROP ALARM (UPSTREAM OUTAGE DETECTOR)
        print(f"🚨 UPSTREAM OUTAGE DETECTED: {len(new_drops)} stations dropped simultaneously (threshold: >{mass_drop_threshold})!")
        print("   Posting critical upstream outage card to Slack...")
        card = build_slack_mass_drop_card(new_drops, latest_ts, avail_pct, counts)
        success = post_to_slack(webhook_url, card, dry_run=dry_run)
        if success and not dry_run:
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            for net, sta, meta, info in new_drops:
                active_outages[sta] = {
                    "network": net,
                    "dropped_at": latest_ts,
                    "alerted_at": now_str,
                    "is_baseline": False,
                    "is_mass_outage": True
                }
            alerts_sent += 1
    else:
        # Normal Individual Station Drops
        weather_hazards = load_weather_hazards()
        for net, sta, meta, info in new_drops:
            weather_info = weather_hazards.get(f"{net}.{sta}", weather_hazards.get(sta))
            card = build_slack_drop_card(net, sta, meta, latest_ts, avail_pct, counts, weather_info=weather_info)
            print(f"🚨 NEW DROP DETECTED: {net}.{sta} ({meta.get('site_name', 'Unknown')})")
            if weather_info:
                print(f"   ⛈️ Active severe weather detected for {sta}: {weather_info.get('event')}")
            print("   Posting alert card to Slack...")

            success = post_to_slack(webhook_url, card, dry_run=dry_run)
            if success and not dry_run:
                active_outages[sta] = {
                    "network": net,
                    "dropped_at": latest_ts,
                    "alerted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "is_baseline": False
                }
                alerts_sent += 1

    # 2. Detect RECOVERIES
    resolved_stations = []
    for sta, out_info in active_outages.items():
        if sta in current_status and current_status[sta]["status"] in ("ONLINE", "DELAYED"):
            resolved_stations.append((sta, out_info))

    if len(resolved_stations) > mass_drop_threshold:
        # Correlated Mass Recovery Notification
        print(f"🟢 MASS RECOVERY DETECTED: {len(resolved_stations)} stations reconnected simultaneously!")
        print("   Posting upstream recovery card to Slack...")
        if notify_recoveries:
            card = build_slack_mass_recovery_card(resolved_stations, latest_ts, avail_pct, counts)
            post_to_slack(webhook_url, card, dry_run=dry_run)
    else:
        for sta, out_info in resolved_stations:
            net = out_info.get("network", "OK")
            meta = metadata.get(f"{net}.{sta}", metadata.get(sta, {}))
            dropped_at = out_info.get("dropped_at", "unknown time")

            duration_str = "approx. 30m+"
            try:
                dt_drop = datetime.fromisoformat(dropped_at.replace('Z', '+00:00'))
                dt_now = datetime.fromisoformat(latest_ts.replace('Z', '+00:00'))
                diff_sec = int((dt_now - dt_drop).total_seconds())
                hrs = diff_sec // 3600
                mins = (diff_sec % 3600) // 60
                duration_str = f"{hrs}h {mins}m" if hrs > 0 else f"{mins}m"
            except Exception:
                pass

            print(f"🟢 STATION RECOVERED: {net}.{sta} (Down for {duration_str})")

            # Only notify if not a chronic baseline outage or recoveries enabled
            if notify_recoveries and not out_info.get("is_baseline", False):
                card = build_slack_recovery_card(net, sta, meta, latest_ts, duration_str)
                print("   Posting recovery card to Slack...")
                post_to_slack(webhook_url, card, dry_run=dry_run)

    # Clean up resolved stations
    if not dry_run:
        for sta, out_info in resolved_stations:
            del active_outages[sta]

        state["last_audit_ts"] = latest_ts
        state["initialized"] = True
        save_alert_state(state, state_file)

    if alerts_sent == 0 and not resolved_stations:
        print(f"ℹ️  No new station state transitions. Active unresolved outages: {len(active_outages)}")

    return alerts_sent


def print_active_alerts(state_file=DEFAULT_STATE_FILE):
    """Prints currently active tracked outages."""
    state = load_alert_state(state_file)
    metadata = load_station_metadata()
    outages = state.get("active_outages", {})

    print("\n=======================================================")
    print(f" 📋 CURRENTLY TRACKED OUTAGES ({len(outages)} stations)")
    print(f" State File : {state_file}")
    print(f" Last Audit : {state.get('last_audit_ts', 'Never')}")
    print("=======================================================")
    if not outages:
        print("  🟢 No active outages tracked. Network healthy!")
    else:
        for sta, info in outages.items():
            net = info.get("network", "OK")
            meta = metadata.get(f"{net}.{sta}", metadata.get(sta, {}))
            site = meta.get("site_name", "Unknown Location")
            drop_time = info.get("dropped_at", "N/A")
            is_base = " [Baseline Outage]" if info.get("is_baseline") else ""
            print(f" • {net}.{sta:<6} : {site:<28} | Down since: {drop_time}{is_base}")
    print("=======================================================\n")


def main():
    parser = argparse.ArgumentParser(
        description="Oklahoma Seismic Network SOH Drop Alerting Script via Slack Webhook."
    )
    parser.add_argument(
        "--webhook-url", "-w",
        default=None,
        help="Slack Webhook URL. Overrides SLACK_WEBHOOK_URL env and webhook file."
    )
    parser.add_argument(
        "--webhook-file",
        default=DEFAULT_WEBHOOK_FILE,
        help=f"File containing Slack Webhook URL (default: {DEFAULT_WEBHOOK_FILE})."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simulate alerting and print JSON payloads without actually posting to Slack."
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Send a test notification card to verify Slack webhook delivery."
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Show currently tracked active alerts and exit."
    )
    parser.add_argument(
        "--clear", action="store_true",
        help="Clear persistent alert state file."
    )
    parser.add_argument(
        "--state-file", default=DEFAULT_STATE_FILE,
        help=f"Path to alert tracking state file (default: {DEFAULT_STATE_FILE})."
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Force alerting on all currently offline stations."
    )
    parser.add_argument(
        "--mass-drop-threshold", type=int, default=DEFAULT_MASS_DROP_THRESHOLD,
        help=f"Threshold of simultaneous station drops to trigger an Upstream Outage Alarm (default: {DEFAULT_MASS_DROP_THRESHOLD})."
    )
    args = parser.parse_args()

    if args.clear:
        if os.path.isfile(args.state_file):
            os.remove(args.state_file)
            print(f"✅ Cleared alert state file: {args.state_file}")
        else:
            print("Alert state file does not exist.")
        return

    if args.status:
        print_active_alerts(state_file=args.state_file)
        return

    # Resolve webhook URL
    webhook_url = resolve_webhook_url(args.webhook_url, args.webhook_file)

    if not webhook_url and not args.dry_run:
        print("❌ Error: No Slack Webhook URL found.")
        print("Provide via --webhook-url, export SLACK_WEBHOOK_URL, or save to 'slack_webhook'.")
        sys.exit(1)

    if args.test:
        print("🚀 Sending test notification card to Slack...")
        card = build_slack_test_card()
        success = post_to_slack(webhook_url, card, dry_run=args.dry_run)
        if success:
            print("✅ Test notification successfully sent to Slack.")
        else:
            print("❌ Failed to send test notification.")
        return

    # Run check and alert
    check_and_alert(
        webhook_url=webhook_url,
        state_file=args.state_file,
        dry_run=args.dry_run,
        force=args.force,
        mass_drop_threshold=args.mass_drop_threshold
    )


if __name__ == "__main__":
    main()
