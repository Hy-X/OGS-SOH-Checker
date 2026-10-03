#!/usr/bin/env python3
"""
verify_streaming.py
===================
Real-time State of Health (SOH) and life-signal monitoring for the Oklahoma Seismic Network.
Performs array-wide bulk waveform queries via FDSNWS (EarthScope) to assess
packet latency, categorize health status (Online, Delayed, Offline), and
maintain persistent audit logs.
"""

import os
import csv
import sys
import time
import json
import argparse
import warnings
from obspy.clients.fdsn import Client
from obspy import UTCDateTime
from obspy.clients.fdsn.header import FDSNNoDataException

# Suppress ObsPy warnings about missing data pieces
warnings.filterwarnings("ignore")


def audit_station_health(csv_file="ogs_stations.csv", window_seconds=60,
                         log_file="monitor_log.csv", write_log=True,
                         json_file=None):
    """
    Performs a bulk FDSN health audit for all stations in the given CSV file.

    Parameters:
        csv_file (str): Path to CSV containing Network and Station columns.
        window_seconds (int): Lookback window in seconds from now.
        log_file (str): Path to CSV log file to append results.
        write_log (bool): Whether to append audit results to log_file.
        json_file (str, optional): Path to output a JSON snapshot of the audit.

    Returns:
        dict: Structured audit result containing counts, station lists, and latency stats.
    """
    stations = []
    try:
        with open(csv_file, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                stations.append((row["Network"], row["Station"]))
    except FileNotFoundError:
        print(f"❌ Error: Could not find '{csv_file}'. Please run query_ogs.sh first.")
        sys.exit(1)

    # EarthScope good citizenship: identify client and enforce timeout
    user_agent = "Oklahoma-Seismic-SOH-Monitor/1.0 (EarthScope Good Citizenship; Automated Telemetry Audit)"
    client = Client("IRIS", timeout=30, user_agent=user_agent)

    # Query the last X seconds to get the most recent packets.
    # This keeps the data payload extremely small and fast, providing true latency.
    now = UTCDateTime()
    t1 = now - window_seconds
    t2 = now

    bulk_request = []
    for net, sta in stations:
        bulk_request.append((net, sta, "*", "*Z", t1, t2))

    try:
        st = client.get_waveforms_bulk(bulk_request)
    except FDSNNoDataException:
        st = []
    except Exception as e:
        print(f"\n❌ Failed to fetch waveforms: {e}")
        st = []

    # Calculate latency per station name across all returned traces.
    # If a station exists in both networks (e.g., OK.SAND and O2.SAND),
    # health is evaluated on the station name, taking the freshest packet endtime.
    station_latest = {}      # sta -> freshest UTCDateTime
    active_network = {}      # sta -> network providing freshest data
    for tr in st:
        sta = tr.stats.station
        net = tr.stats.network
        if sta not in station_latest or tr.stats.endtime > station_latest[sta]:
            station_latest[sta] = tr.stats.endtime
            active_network[sta] = net

    # Map unique stations to their inventory networks (preserving order)
    station_inventory = {}   # sta -> list of networks
    for net, sta in stations:
        if sta not in station_inventory:
            station_inventory[sta] = []
        if net not in station_inventory[sta]:
            station_inventory[sta].append(net)

    # Classify unique stations for the life-signal monitor
    online = []
    delayed = []
    offline = []

    for sta, net_list in station_inventory.items():
        if sta in station_latest:
            latency = float(now - station_latest[sta])
            net = active_network.get(sta, net_list[0])
            if latency <= 30.0:
                online.append((net, sta, latency))
            else:
                delayed.append((net, sta, latency))
        else:
            default_net = net_list[0]
            offline.append((default_net, sta, None))

    # Sort lists
    online.sort(key=lambda x: x[2])
    delayed.sort(key=lambda x: x[2])
    offline.sort(key=lambda x: (x[0], x[1]))

    total_stations = len(station_inventory)
    active_streaming = len(online) + len(delayed)
    availability_pct = round((active_streaming / total_stations * 100), 1) if total_stations > 0 else 0.0

    timestamp_str = now.strftime('%Y-%m-%dT%H:%M:%SZ')

    # Build per-inventory records so Roster and Map retain all station entries with resolved health
    inventory_records = []
    for net, sta in stations:
        if sta in station_latest:
            lat = float(now - station_latest[sta])
            st_val = "ONLINE" if lat <= 30.0 else "DELAYED"
            inventory_records.append({
                "network": net, "station": sta, "status": st_val, "latency_s": round(lat, 2)
            })
        else:
            inventory_records.append({
                "network": net, "station": sta, "status": "OFFLINE", "latency_s": None
            })

    # Append to monitor_log.csv
    if write_log and log_file:
        log_exists = os.path.isfile(log_file)
        with open(log_file, 'a', newline='', encoding='utf-8') as lf:
            writer = csv.writer(lf)
            if not log_exists:
                writer.writerow(["Timestamp", "Network", "Station", "Status", "Latency_s"])

            for rec in inventory_records:
                lat_str = rec["latency_s"] if rec["latency_s"] is not None else ""
                writer.writerow([timestamp_str, rec["network"], rec["station"], rec["status"], lat_str])

    # Build structured result dictionary
    result = {
        "timestamp": timestamp_str,
        "window_seconds": window_seconds,
        "counts": {
            "total": total_stations,
            "online": len(online),
            "delayed": len(delayed),
            "offline": len(offline),
        },
        "availability_pct": availability_pct,
        "online": [{"network": n, "station": s, "latency_s": round(l, 2)} for n, s, l in online],
        "delayed": [{"network": n, "station": s, "latency_s": round(l, 2)} for n, s, l in delayed],
        "offline": [{"network": n, "station": s} for n, s, _ in offline],
        "inventory": inventory_records
    }

    # Optionally write JSON snapshot
    if json_file:
        with open(json_file, 'w', encoding='utf-8') as jf:
            json.dump(result, jf, indent=2)

    return result, online, delayed, offline, now


def print_dashboard(result, online, delayed, offline, now, window_seconds, log_file):
    """Prints a formatted CLI dashboard."""
    if sys.stdout.isatty():
        print("\033[2J\033[H")  # Clear screen in interactive terminal only
    print("=========================================================")
    print(" 📡 OKLAHOMA SEISMIC NETWORK REAL-TIME LIFE SIGNAL MONITOR")
    print(f" Time: {now.strftime('%Y-%m-%d %H:%M:%S UTC')} | Window: Last {window_seconds}s")
    print("=========================================================")
    print(f" 🟢 ONLINE  (<30s latency) : {len(online)}")
    print(f" 🟡 DELAYED (>30s latency) : {len(delayed)}")
    print(f" 🔴 OFFLINE (No recent data): {len(offline)}")
    print(f" 📈 Availability Rate      : {result['availability_pct']}%")
    if log_file:
        print(f" 💾 Log Appended           : {log_file}")
    print("=========================================================\n")

    print("--- 🔴 OFFLINE STATIONS ---")
    if offline:
        print(", ".join([f"{n}.{s}" for n, s, _ in offline]))
    else:
        print("None! All stations are streaming.")

    print("\n--- 🟡 DELAYED STATIONS ---")
    if delayed:
        for n, s, lat in delayed:
            print(f" {n}.{s:<5} : {lat:.1f}s delayed")
    else:
        print("None!")

    print("\n--- 🟢 ONLINE STATIONS (Top 15 Samples) ---")
    if online:
        sample = online[:15]
        for n, s, lat in sample:
            print(f" {n}.{s:<5} : {lat:.1f}s latency")
        if len(online) > 15:
            print(f" ... plus {len(online) - 15} more stations with low latency.")
    else:
        print("None!")


def check_life_signals(csv_file="ogs_stations.csv", window_seconds=60,
                       log_file="monitor_log.csv", write_log=True,
                       json_file=None, quiet=False):
    """Audit station health and optionally print dashboard."""
    result, online, delayed, offline, now = audit_station_health(
        csv_file=csv_file,
        window_seconds=window_seconds,
        log_file=log_file,
        write_log=write_log,
        json_file=json_file
    )
    if not quiet:
        print_dashboard(result, online, delayed, offline, now, window_seconds, log_file)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Oklahoma Seismic Network real-time life signal monitor.")
    parser.add_argument("--csv", default="ogs_stations.csv", help="Station CSV file (default: ogs_stations.csv)")
    parser.add_argument("--window", type=int, default=60, help="Check window in seconds (default: 60)")
    parser.add_argument("--loop", type=int, help="Run continuously every X seconds")
    parser.add_argument("--json", dest="json_file", help="Export latest snapshot to JSON file")
    parser.add_argument("--no-log", action="store_true", help="Do not append to monitor_log.csv")
    parser.add_argument("--quiet", action="store_true", help="Do not print terminal dashboard")
    args = parser.parse_args()

    write_log = not args.no_log

    if args.loop:
        try:
            while True:
                check_life_signals(
                    csv_file=args.csv,
                    window_seconds=args.window,
                    write_log=write_log,
                    json_file=args.json_file,
                    quiet=args.quiet
                )
                time.sleep(args.loop)
        except KeyboardInterrupt:
            print("\nMonitoring stopped.")
    else:
        check_life_signals(
            csv_file=args.csv,
            window_seconds=args.window,
            write_log=write_log,
            json_file=args.json_file,
            quiet=args.quiet
        )
