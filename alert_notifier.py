#!/usr/bin/env python3
"""
alert_notifier.py
=================
Automated State of Health (SOH) alert notification script for the Oklahoma Seismic Network.
Detects when a seismic station drops (Online -> Offline) or recovers (Offline -> Online)
and sends immediate email notifications via `s-nail`.

Features:
  - Deduplicated alerting via persistent state file (.alert_state.json) to prevent email spam.
  - Automatically suppresses alerts for chronic/baseline offline stations on initial run.
  - Formats detailed, actionable telemetry drop and recovery email notifications.
  - Supports --dry-run for testing logic without dispatching emails.
  - Supports --test to verify s-nail transmission to your inbox.
"""

import os
import sys
import csv
import json
import subprocess
import argparse
from datetime import datetime, timezone


DEFAULT_STATE_FILE = ".alert_state.json"
DEFAULT_META_FILE = "ogs_stations.csv"
DEFAULT_STATUS_FILE = "soh_status.json"
DEFAULT_LOG_FILE = "monitor_log.csv"
DEFAULT_WEATHER_FILE = "weather_alerts.json"
DEFAULT_RECIPIENT = "YOUREMAIL@YOURADDRESS"
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


def send_email_via_snail(recipient, subject, body, dry_run=False):
    """Invokes s-nail to dispatch an email."""
    if dry_run:
        print("\n================ [DRY-RUN EMAIL NOTIFICATION] ================")
        print(f"To      : {recipient}")
        print(f"Subject : {subject}")
        print("---------------------------------------------------------------")
        print(body)
        print("===============================================================\n")
        return True

    try:
        proc = subprocess.run(
            ["s-nail", "-s", subject, recipient],
            input=body.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True
        )
        return True
    except FileNotFoundError:
        sys.stderr.write("Error: 's-nail' executable not found in PATH.\n")
        return False
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode("utf-8", errors="replace")
        sys.stderr.write(f"Error: s-nail failed (exit code {e.returncode}): {err}\n")
        return False


def build_drop_email(net, sta, meta, dropped_at, avail_pct, counts, weather_info=None):
    """Formats the alert message when a station drops."""
    site_name = meta.get("site_name", "Unknown Location")
    lat = meta.get("latitude", "N/A")
    lon = meta.get("longitude", "N/A")
    elev = meta.get("elevation_m", "N/A")

    subject = f"🚨 [SOH ALERT] Seismic Station Down: {net}.{sta} ({site_name})"

    weather_section = ""
    if weather_info:
        ev = weather_info.get("event", "Severe Weather Warning")
        off = weather_info.get("office", "NWS")
        exp = weather_info.get("expires", "N/A")
        weather_section = f"""----------------------------------------------------------------------
⛈️ SEVERE WEATHER CORRELATION NOTICE
----------------------------------------------------------------------
Station is currently located inside an active NWS Severe Weather Warning:
Hazard Event : {ev}
Issuing Office: {off}
Valid Until  : {exp}
Note         : Station telemetry loss is likely storm-, lightning-, or
               commercial power/cell tower disruption related.
SAFETY ALERT : Defer technician field visits until severe weather passes.

"""

    body = f"""======================================================================
  🚨 OKLAHOMA SEISMIC NETWORK STATE OF HEALTH ALERT
  Station Telemetry Drop Detected
======================================================================

Station ID      : {net}.{sta}
Site Name       : {site_name}
Coordinates     : Latitude {lat}, Longitude {lon} (Elevation: {elev} m)
Time Detected   : {dropped_at} UTC
Current Status  : 🔴 OFFLINE (No incoming telemetry packets)

----------------------------------------------------------------------
ARRAY STATE OF HEALTH IMPACT
----------------------------------------------------------------------
Total Monitored : {counts.get('total', 'N/A')} stations
🟢 Online       : {counts.get('online', 'N/A')}
🟡 Delayed      : {counts.get('delayed', 'N/A')}
🔴 Offline      : {counts.get('offline', 'N/A')}
Network Avail   : {avail_pct}%

{weather_section}----------------------------------------------------------------------
FIELD DIAGNOSTIC CHECKLIST
----------------------------------------------------------------------
1. Power & Battery:
   - Check solar panel output voltage and charge controller status.
   - Inspect 12V AGM battery health (low-voltage disconnect).

2. Cellular Modem & Telemetry:
   - Check cell carrier connectivity and signal strength (RSSI / RSRP).
   - Check antenna cable connections and alignment.
   - Initiate remote cellular modem power cycle if accessible.

3. Digitizer & Seismic Sensor:
   - Verify digitizer link and SeedLink buffer state.
   - Inspect data logger connection port.

----------------------------------------------------------------------
This is an automated telemetry alert from the Oklahoma Seismic Network
State of Health Monitoring Platform.
Dashboard: http://localhost:8050/soh_dashboard.html
======================================================================
"""
    return subject, body


def build_mass_drop_email(drops, dropped_at, avail_pct, counts):
    """Formats an email notification for a correlated mass drop / upstream outage."""
    count = len(drops)
    subject = f"🚨 [CRITICAL SOH ALERT] Upstream Outage: {count} Stations Dropped Simultaneously"

    station_lines = []
    for net, sta, meta, info in drops:
        site = meta.get("site_name", "Unknown Location")
        lat = meta.get("latitude", "N/A")
        lon = meta.get("longitude", "N/A")
        station_lines.append(f"  • {net}.{sta:<6} : {site:<30} ({lat}, {lon})")

    stations_block = "\n".join(station_lines)

    body = f"""======================================================================
  🚨 OKLAHOMA SEISMIC NETWORK STATE OF HEALTH CRITICAL ALERT
  UPSTREAM OUTAGE DETECTED (Correlated Mass Drop Alarm)
======================================================================

CRITICAL INCIDENT SUMMARY:
  Simultaneous Drops : {count} stations went offline in the exact same audit cycle
  Audit Timestamp    : {dropped_at} UTC
  Network Avail      : {avail_pct}%
  Array State        : 🟢 {counts.get('online', 0)} Online | 🟡 {counts.get('delayed', 0)} Delayed | 🔴 {counts.get('offline', 0)} Offline

----------------------------------------------------------------------
CORRELATED FAILURE ANALYSIS
----------------------------------------------------------------------
A simultaneous telemetry drop across {count} stations indicates an upstream
infrastructure disruption rather than isolated hardware faults at field sites.

Probable Root Causes:
  1. EarthScope IRIS DMC Data Center disruption (FDSNWS / SeedLink server outage).
  2. Regional cellular carrier backbone disruption (e.g. AT&T / Verizon FirstNet).
  3. Monitoring host network interface, default gateway, or DNS resolution failure.

----------------------------------------------------------------------
AFFECTED SEISMIC STATIONS ({count})
----------------------------------------------------------------------
{stations_block}

----------------------------------------------------------------------
UPSTREAM ACTION CHECKLIST
----------------------------------------------------------------------
1. Verify EarthScope Data Services:
   - Check http://service.iris.edu/fdsnws/dataselect/1/
   - Check SeedLink ring server at rtserve.earthscope.org:18000
2. Verify Monitoring Host Network Route:
   - ping -c 3 service.iris.edu
   - traceroute service.iris.edu
3. Check Regional Cellular Carrier Outage Maps:
   - Oklahoma statewide wireless coverage reports.
4. Suppressed Individual Alerts:
   - Individual station alert emails have been suppressed for this cycle
     to prevent inbox flooding.

======================================================================
Oklahoma Seismic Network SOH Telemetry Platform
Dashboard: http://localhost:8050/soh_dashboard.html
======================================================================
"""
    return subject, body


def build_mass_recovery_email(recoveries, restored_at, avail_pct, counts):
    """Formats an email notification for an upstream mass recovery notice."""
    count = len(recoveries)
    subject = f"🟢 [SOH RECOVERED] Upstream Connection Restored: {count} Stations Reconnected"

    station_lines = []
    for sta, out_info in recoveries:
        net = out_info.get("network", "OK")
        dropped_at = out_info.get("dropped_at", "unknown")
        station_lines.append(f"  • {net}.{sta:<6} : Telemetry Resumed (Offline since: {dropped_at})")

    stations_block = "\n".join(station_lines)

    body = f"""======================================================================
  🟢 OKLAHOMA SEISMIC NETWORK TELEMETRY RESTORATION NOTICE
  UPSTREAM CONNECTION RESTORED ({count} Stations Reconnected)
======================================================================

INCIDENT RESTORATION SUMMARY:
  Reconnected Sites  : {count} stations resumed telemetry simultaneously
  Restored Timestamp : {restored_at} UTC
  Network Avail      : {avail_pct}%
  Array State        : 🟢 {counts.get('online', 0)} Online | 🟡 {counts.get('delayed', 0)} Delayed | 🔴 {counts.get('offline', 0)} Offline

----------------------------------------------------------------------
RECONNECTED SEISMIC STATIONS ({count})
----------------------------------------------------------------------
{stations_block}

----------------------------------------------------------------------
STATUS:
Data flow from EarthScope / regional cellular carriers is back to normal
operational status. No further field intervention is required.

======================================================================
Oklahoma Seismic Network SOH Telemetry Platform
Dashboard: http://localhost:8050/soh_dashboard.html
======================================================================
"""
    return subject, body


def build_recovery_email(net, sta, meta, restored_at, down_duration_str):
    """Formats the notification when a station recovers."""
    site_name = meta.get("site_name", "Unknown Location")

    subject = f"🟢 [SOH RECOVERED] Seismic Station Restored: {net}.{sta} ({site_name})"

    body = f"""======================================================================
  🟢 OKLAHOMA SEISMIC NETWORK TELEMETRY RECOVERY NOTICE
  Station Communication Restored
======================================================================

Station ID      : {net}.{sta}
Site Name       : {site_name}
Restored At     : {restored_at} UTC
Previous Outage : {down_duration_str}
Current Status  : 🟢 ONLINE (Normal telemetry streaming resumed)

Telemetry packets are once again arriving within the low-latency window.
No further field action is required at this time.

----------------------------------------------------------------------
This is an automated notification from the Oklahoma Seismic Network
State of Health Monitoring Platform.
======================================================================
"""
    return subject, body


def check_and_alert(recipients, state_file=DEFAULT_STATE_FILE, dry_run=False,
                    notify_recoveries=True, force=False,
                    mass_drop_threshold=DEFAULT_MASS_DROP_THRESHOLD):
    """
    Main logic: Compares latest audit against alert state, dispatches alerts
    for new drops and recoveries, and updates state file.
      - Correlated / Mass Drop Alarm: Dispatches Upstream Outage Alert when > mass_drop_threshold stations drop simultaneously.
      - Normal Drop Alerts: Individual station drop emails when <= mass_drop_threshold.
      - Detects RECOVERIES (Offline -> Online) with mass recovery grouping.
    """
    metadata = load_station_metadata()
    latest_ts, current_status, avail_pct, counts = load_latest_telemetry()

    if not current_status:
        sys.stderr.write("Notice: No telemetry data available to evaluate alerts.\n")
        return 0

    state = load_alert_state(state_file)
    active_outages = state.setdefault("active_outages", {})
    is_initial_run = not state.get("initialized", False)

    # On the very first run, seed active_outages with all currently offline stations
    # so we DO NOT spam the user with 10 emails for historical chronic outages!
    if is_initial_run and not force:
        print("ℹ️  Initial run: Seeding baseline offline stations into alert state (suppressing legacy alerts)...")
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
    recipients_list = [r.strip() for r in recipients.split(',') if r.strip()]

    # 1. Detect NEW DROPS (Online in previous state -> Offline now)
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
        subject, body = build_mass_drop_email(new_drops, latest_ts, avail_pct, counts)
        all_ok = True
        for rec in recipients_list:
            print(f"   Dispatching upstream outage alert to: {rec}")
            success = send_email_via_snail(rec, subject, body, dry_run=dry_run)
            if not success:
                all_ok = False

        if all_ok and not dry_run:
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
            subject, body = build_drop_email(net, sta, meta, latest_ts, avail_pct, counts, weather_info=weather_info)
            print(f"🚨 NEW DROP DETECTED: {net}.{sta} ({meta.get('site_name', 'Unknown')})")
            if weather_info:
                print(f"   ⛈️ Active severe weather detected for {sta}: {weather_info.get('event')}")

            all_ok = True
            for rec in recipients_list:
                print(f"   Dispatching alert to: {rec}")
                success = send_email_via_snail(rec, subject, body, dry_run=dry_run)
                if not success:
                    all_ok = False

            if all_ok and not dry_run:
                active_outages[sta] = {
                    "network": net,
                    "dropped_at": latest_ts,
                    "alerted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "is_baseline": False
                }
                alerts_sent += 1

    # 2. Detect RECOVERIES (Was down in active_outages -> Online now)
    resolved_stations = []
    for sta, out_info in active_outages.items():
        if sta in current_status and current_status[sta]["status"] in ("ONLINE", "DELAYED"):
            resolved_stations.append((sta, out_info))

    if len(resolved_stations) > mass_drop_threshold:
        # Correlated Mass Recovery Notification
        print(f"🟢 MASS RECOVERY DETECTED: {len(resolved_stations)} stations reconnected simultaneously!")
        if notify_recoveries:
            subject, body = build_mass_recovery_email(resolved_stations, latest_ts, avail_pct, counts)
            for rec in recipients_list:
                print(f"   Dispatching mass recovery notice to: {rec}")
                send_email_via_snail(rec, subject, body, dry_run=dry_run)
    else:
        for sta, out_info in resolved_stations:
            net = out_info.get("network", "OK")
            meta = metadata.get(f"{net}.{sta}", metadata.get(sta, {}))
            dropped_at = out_info.get("dropped_at", "unknown time")

            # Calculate downtime
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

            # Only send recovery email if it wasn't a baseline pre-existing offline station
            # or if notify_recoveries is enabled
            if notify_recoveries and not out_info.get("is_baseline", False):
                subject, body = build_recovery_email(net, sta, meta, latest_ts, duration_str)
                for rec in recipients_list:
                    print(f"   Dispatching recovery notice to: {rec}")
                    send_email_via_snail(rec, subject, body, dry_run=dry_run)

    # Remove resolved stations from active outages
    if not dry_run:
        for sta, out_info in resolved_stations:
            del active_outages[sta]

        state["last_audit_ts"] = latest_ts
        state["initialized"] = True
        save_alert_state(state, state_file)

    if alerts_sent == 0 and not resolved_stations:
        print(f"ℹ️  No new station state transitions. Active unresolved outages: {len(active_outages)}")

    return alerts_sent


def send_test_email(recipient, dry_run=False):
    """Sends a mock test email to verify s-nail configuration."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    subject = f"🧪 [SOH TEST] Oklahoma Seismic Network Alert System Verification ({now_str})"
    body = f"""======================================================================
  🧪 OKLAHOMA SEISMIC NETWORK ALERT SYSTEM - CONFIGURATION TEST
======================================================================

This is a test notification confirming that the s-nail email alert system
is properly configured and operational on this host.

Host Timestamp: {now_str}
Transport     : /usr/bin/s-nail
Alert Engine  : alert_notifier.py

If you received this message, station telemetry drop and recovery alerts
will be successfully delivered to your inbox.
======================================================================
"""
    print(f"Sending test notification to: {recipient}...")
    success = send_email_via_snail(recipient, subject, body, dry_run=dry_run)
    if success:
        print("✅ Test email successfully submitted to s-nail.")
    else:
        print("❌ Test email submission failed.")
    return success


def print_active_alerts(state_file=DEFAULT_STATE_FILE):
    """Prints the currently tracked active outages in terminal."""
    state = load_alert_state(state_file)
    outages = state.get("active_outages", {})
    metadata = load_station_metadata()

    print(f"\n=======================================================")
    print(f" 📡 ACTIVE STATION OUTAGE ALERTS ({len(outages)} tracked)")
    print(f"=======================================================")
    if not outages:
        print(" All stations are nominal or no active outages tracked.")
    else:
        for sta, info in sorted(outages.items()):
            net = info.get("network", "OK")
            meta = metadata.get(f"{net}.{sta}", metadata.get(sta, {}))
            site = meta.get("site_name", "Unknown Location")
            drop_time = info.get("dropped_at", "N/A")
            is_base = " [Baseline Outage]" if info.get("is_baseline") else ""
            print(f" • {net}.{sta:<6} : {site:<28} | Down since: {drop_time}{is_base}")
    print("=======================================================\n")


def main():
    parser = argparse.ArgumentParser(
        description="Oklahoma Seismic Network SOH Drop Alerting Script (via s-nail)."
    )
    parser.add_argument(
        "--recipient", "-r",
        default=os.environ.get("ALERT_EMAIL", DEFAULT_RECIPIENT),
        help=f"Recipient email address(es), comma-separated (default: {DEFAULT_RECIPIENT}). Can also be set via ALERT_EMAIL."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simulate alerting without actually sending emails via s-nail."
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Send a test notification to verify s-nail delivery."
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Show currently tracked active alerts and exit."
    )
    parser.add_argument(
        "--clear", action="store_true",
        help="Clear persistent alert state (.alert_state.json)."
    )
    parser.add_argument(
        "--state-file", default=DEFAULT_STATE_FILE,
        help=f"Path to alert tracking file (default: {DEFAULT_STATE_FILE})."
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Force alerting on all currently offline stations even on initial run."
    )
    parser.add_argument(
        "--mass-drop-threshold", type=int, default=DEFAULT_MASS_DROP_THRESHOLD,
        help=f"Threshold for correlated/mass drop alarm (default: {DEFAULT_MASS_DROP_THRESHOLD})."
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

    if not args.recipient:
        print("❌ Error: No recipient email specified.")
        print("Please provide --recipient user@example.com or export ALERT_EMAIL='user@example.com'")
        sys.exit(1)

    if args.test:
        send_test_email(recipient=args.recipient, dry_run=args.dry_run)
        return

    # Check for drops and send alerts
    check_and_alert(
        recipients=args.recipient,
        state_file=args.state_file,
        dry_run=args.dry_run,
        force=args.force,
        mass_drop_threshold=args.mass_drop_threshold
    )


if __name__ == "__main__":
    main()
