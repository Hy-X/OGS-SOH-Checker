#!/usr/bin/env python3
"""
station_analyzer.py
===================
Operational telemetry analysis routine for the Oklahoma Seismic Network.
Analyzes temporal telemetry logs (monitor_log.csv or cron.log) to:
  1. Estimate station situations (newly down, flapping, chronic offline, recovered, delayed).
  2. Compute network availability and latency metrics.
  3. Compile actionable, concise State of Health (SOH) summary reports (Markdown, Terminal, JSON).
"""

import os
import sys
import csv
import json
import re
import argparse
from datetime import datetime, timezone
from collections import defaultdict


class StationAnalyzer:
    """Analyzes seismic station health history and generates operational reports."""

    def __init__(self, meta_file="ogs_stations.csv"):
        self.meta_file = meta_file
        self.metadata = self._load_metadata()
        self.timestamps = []
        self.records_by_ts = defaultdict(dict)  # ts -> sta -> {'network': net, 'status': st, 'latency': lat}
        self.analysis = {}

    def _load_metadata(self):
        """Loads station geographic and site metadata."""
        meta = {}
        if not os.path.isfile(self.meta_file):
            return meta

        try:
            with open(self.meta_file, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    net = row.get("Network", "").strip()
                    sta = row.get("Station", "").strip()
                    key = f"{net}.{sta}" if net and sta else sta
                    meta[key] = {
                        "network": net,
                        "station": sta,
                        "site_name": row.get("SiteName", "Unknown").strip(),
                        "latitude": row.get("Latitude", ""),
                        "longitude": row.get("Longitude", ""),
                        "elevation_m": row.get("Elevation_m", "")
                    }
                    # Also store indexed by station name alone for fallback lookup
                    if sta not in meta:
                        meta[sta] = meta[key]
        except Exception as e:
            sys.stderr.write(f"Warning: Failed reading metadata file {self.meta_file}: {e}\n")
        return meta

    def get_meta(self, net, sta):
        """Returns metadata dictionary for a station."""
        key = f"{net}.{sta}"
        if key in self.metadata:
            return self.metadata[key]
        if sta in self.metadata:
            return self.metadata[sta]
        return {
            "network": net,
            "station": sta,
            "site_name": "Unknown",
            "latitude": "",
            "longitude": "",
            "elevation_m": ""
        }

    def load_from_monitor_log(self, log_file="monitor_log.csv", max_hours=None, max_runs=None):
        """Loads telemetry records from monitor_log.csv."""
        if not os.path.isfile(log_file):
            raise FileNotFoundError(f"Monitor log file not found: {log_file}")

        records_raw = []
        with open(log_file, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                records_raw.append(row)

        if not records_raw:
            return False

        # Group records by timestamp
        ts_groups = defaultdict(dict)
        for r in records_raw:
            ts = r.get("Timestamp", "").strip()
            net = r.get("Network", "").strip()
            sta = r.get("Station", "").strip()
            status = r.get("Status", "").strip().upper()
            lat_str = r.get("Latency_s", "").strip()
            latency = float(lat_str) if lat_str else None

            # Resolve station health (if duplicate records exist for a station name, take best status)
            if sta in ts_groups[ts]:
                curr = ts_groups[ts][sta]
                # Prioritize ONLINE over DELAYED over OFFLINE
                curr_st = curr["status"]
                if curr_st == "OFFLINE" and status in ("ONLINE", "DELAYED"):
                    ts_groups[ts][sta] = {"network": net, "status": status, "latency": latency}
                elif curr_st == "DELAYED" and status == "ONLINE":
                    ts_groups[ts][sta] = {"network": net, "status": status, "latency": latency}
                elif curr_st == status and latency is not None:
                    if curr["latency"] is None or latency < curr["latency"]:
                        ts_groups[ts][sta] = {"network": net, "status": status, "latency": latency}
            else:
                ts_groups[ts][sta] = {"network": net, "status": status, "latency": latency}

        all_ts = sorted(list(ts_groups.keys()))
        if not all_ts:
            return False

        # Apply filtering
        selected_ts = self._filter_timestamps(all_ts, max_hours, max_runs)
        self.timestamps = selected_ts
        self.records_by_ts = {ts: ts_groups[ts] for ts in selected_ts}
        return True

    def load_from_cron_log(self, cron_file="cron.log", max_hours=None, max_runs=None):
        """Parses audit records directly from cron.log text output."""
        if not os.path.isfile(cron_file):
            raise FileNotFoundError(f"Cron log file not found: {cron_file}")

        with open(cron_file, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()

        # Split into workflow runs
        runs = content.split('Oklahoma Seismic Network State of Health & Stream Visualization')[1:]
        if not runs:
            return False

        time_re = re.compile(r'Time: ([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}) UTC')
        off_re = re.compile(r'--- 🔴 OFFLINE STATIONS ---\n([^\n]+)')
        del_re = re.compile(r'--- 🟡 DELAYED STATIONS ---\n([^\n]+)')

        # Collect inventory station names from metadata if available
        known_stations = set()
        for k, m in self.metadata.items():
            if m.get("station"):
                known_stations.add((m.get("network", "OK"), m.get("station")))

        ts_groups = defaultdict(dict)
        for r in runs:
            tm_match = time_re.search(r)
            if not tm_match:
                continue
            raw_time = tm_match.group(1)
            # Standardize timestamp to ISO UTC string
            dt = datetime.strptime(raw_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            ts = dt.strftime("%Y-%m-%dT%H:%M:%SZ")

            off_match = off_re.search(r)
            del_match = del_re.search(r)

            offline_set = set()
            if off_match and off_match.group(1).strip() != "None!":
                for item in off_match.group(1).split(','):
                    s = item.strip()
                    if s:
                        offline_set.add(s)

            delayed_set = set()
            if del_match and del_match.group(1).strip() != "None!":
                for item in del_match.group(1).split(','):
                    s = item.strip()
                    if s:
                        delayed_set.add(s)

            for net, sta in known_stations:
                full_id = f"{net}.{sta}"
                if full_id in offline_set or sta in offline_set:
                    st = "OFFLINE"
                elif full_id in delayed_set or sta in delayed_set:
                    st = "DELAYED"
                else:
                    st = "ONLINE"
                ts_groups[ts][sta] = {"network": net, "status": st, "latency": 0.0 if st == "ONLINE" else None}

        all_ts = sorted(list(ts_groups.keys()))
        if not all_ts:
            return False

        selected_ts = self._filter_timestamps(all_ts, max_hours, max_runs)
        self.timestamps = selected_ts
        self.records_by_ts = {ts: ts_groups[ts] for ts in selected_ts}
        return True

    def _filter_timestamps(self, all_ts, max_hours, max_runs):
        """Helper to slice timestamps according to lookback window constraints."""
        if not all_ts:
            return []

        filtered = all_ts

        if max_hours is not None and max_hours > 0:
            try:
                # Parse latest timestamp
                latest_dt = datetime.fromisoformat(all_ts[-1].replace('Z', '+00:00'))
                cutoff_dt = latest_dt.timestamp() - (max_hours * 3600)
                filtered = [
                    t for t in all_ts
                    if datetime.fromisoformat(t.replace('Z', '+00:00')).timestamp() >= cutoff_dt
                ]
            except Exception:
                pass

        if max_runs is not None and max_runs > 0:
            filtered = filtered[-max_runs:]

        return filtered

    def analyze(self):
        """Performs comprehensive health estimation across all monitored stations."""
        if not self.timestamps:
            return {}

        all_stations = sorted(list(set(
            sta for ts in self.timestamps for sta in self.records_by_ts[ts].keys()
        )))

        latest_ts = self.timestamps[-1]
        first_ts = self.timestamps[0]
        total_cycles = len(self.timestamps)

        # Network availability over time
        cycle_metrics = []
        for ts in self.timestamps:
            records = self.records_by_ts[ts]
            online_cnt = sum(1 for r in records.values() if r["status"] == "ONLINE")
            delayed_cnt = sum(1 for r in records.values() if r["status"] == "DELAYED")
            offline_cnt = sum(1 for r in records.values() if r["status"] == "OFFLINE")
            total = len(records)
            avail_pct = round(((online_cnt + delayed_cnt) / total * 100), 2) if total > 0 else 0.0
            cycle_metrics.append({
                "timestamp": ts,
                "total": total,
                "online": online_cnt,
                "delayed": delayed_cnt,
                "offline": offline_cnt,
                "availability_pct": avail_pct
            })

        station_assessments = {}

        for sta in all_stations:
            history = []
            latencies = []
            net = "OK"

            for ts in self.timestamps:
                rec = self.records_by_ts[ts].get(sta)
                if rec:
                    net = rec["network"]
                    history.append((ts, rec["status"]))
                    if rec["latency"] is not None:
                        latencies.append(rec["latency"])
                else:
                    history.append((ts, "UNKNOWN"))

            statuses = [st for _, st in history]
            current_status = statuses[-1]
            first_status = statuses[0]

            online_cycles = sum(1 for s in statuses if s == "ONLINE")
            delayed_cycles = sum(1 for s in statuses if s == "DELAYED")
            offline_cycles = sum(1 for s in statuses if s == "OFFLINE")
            valid_cycles = online_cycles + delayed_cycles + offline_cycles
            uptime_pct = round(((online_cycles + delayed_cycles) / valid_cycles * 100), 1) if valid_cycles > 0 else 0.0

            # State transitions
            transitions = []
            for i in range(1, len(history)):
                prev_ts, prev_st = history[i-1]
                curr_ts, curr_st = history[i]
                if prev_st != curr_st and prev_st != "UNKNOWN" and curr_st != "UNKNOWN":
                    transitions.append({
                        "timestamp": curr_ts,
                        "from": prev_st,
                        "to": curr_st
                    })

            # Calculate current outage or online duration
            current_run_length = 0
            for s in reversed(statuses):
                if s == current_status:
                    current_run_length += 1
                else:
                    break

            # Estimate duration in hours/minutes (based on timestamp differences)
            duration_str = self._calc_duration_string(history, current_run_length)

            # Health classification category:
            # - CHRONIC_OFFLINE: offline for 100% of the window
            # - NEWLY_DOWN / ACTIVE_OUTAGE: currently offline, but was online earlier in window
            # - DELAYED: currently delayed (>30s latency)
            # - FLAPPING: currently online, but had frequent state transitions (>= 3)
            # - RECOVERED: currently online, had a brief outage earlier in window
            # - STABLE_ONLINE: 100% online
            if all(s == "OFFLINE" for s in statuses):
                category = "CHRONIC_OFFLINE"
                diagnosis = "Permanently silent across entire audit window (no telemetry packets received)."
                priority = "P3 (Scheduled Maintenance / Backlog)"
            elif current_status == "OFFLINE":
                category = "NEWLY_DOWN"
                last_drop_ts = next((t["timestamp"] for t in reversed(transitions) if t["to"] == "OFFLINE"), latest_ts)
                flap_note = f" ({len(transitions)} state toggles observed)" if len(transitions) >= 3 else ""
                diagnosis = f"Telemetry dropped at {last_drop_ts}. Currently silent for {duration_str}{flap_note}."
                priority = "P1 (Urgent: Telemetry Outage)"
            elif current_status == "DELAYED":
                category = "DELAYED"
                diagnosis = f"Experiencing packet delivery lag (>30s latency) for {duration_str}."
                priority = "P2 (Moderate: Telemetry Lag)"
            elif len(transitions) >= 3:
                category = "FLAPPING"
                diagnosis = f"Intermittent link flapping: {len(transitions)} state changes during window. Currently restored ({duration_str})."
                priority = "P2 (Watchlist: Unstable Telemetry)"
            elif any(s == "OFFLINE" for s in statuses):
                category = "RECOVERED"
                last_recovery_ts = next((t["timestamp"] for t in reversed(transitions) if t["to"] == "ONLINE"), latest_ts)
                diagnosis = f"Recovered at {last_recovery_ts}. Operating stably for {duration_str}."
                priority = "P4 (Informational: Recovered)"
            else:
                category = "STABLE_ONLINE"
                diagnosis = "Continuous nominal real-time telemetry streaming."
                priority = "Nominal"

            meta = self.get_meta(net, sta)

            avg_lat = round(sum(latencies) / len(latencies), 2) if latencies else None
            max_lat = round(max(latencies), 2) if latencies else None

            station_assessments[sta] = {
                "network": net,
                "station": sta,
                "site_name": meta.get("site_name", "Unknown"),
                "latitude": meta.get("latitude"),
                "longitude": meta.get("longitude"),
                "current_status": current_status,
                "category": category,
                "priority": priority,
                "diagnosis": diagnosis,
                "uptime_pct": uptime_pct,
                "transitions_count": len(transitions),
                "transitions": transitions,
                "current_streak_cycles": current_run_length,
                "duration_str": duration_str,
                "avg_latency_s": avg_lat,
                "max_latency_s": max_lat,
            }

        # Summary KPIs
        latest_metric = cycle_metrics[-1]
        first_metric = cycle_metrics[0]
        avail_change = round(latest_metric["availability_pct"] - first_metric["availability_pct"], 2)

        categories_count = defaultdict(int)
        for sa in station_assessments.values():
            categories_count[sa["category"]] += 1

        self.analysis = {
            "window": {
                "first_timestamp": first_ts,
                "latest_timestamp": latest_ts,
                "total_cycles": total_cycles,
                "duration_hours": self._calc_hours_between(first_ts, latest_ts)
            },
            "kpi": {
                "total_stations": latest_metric["total"],
                "current_online": latest_metric["online"],
                "current_delayed": latest_metric["delayed"],
                "current_offline": latest_metric["offline"],
                "current_availability_pct": latest_metric["availability_pct"],
                "window_start_availability_pct": first_metric["availability_pct"],
                "availability_change_pct": avail_change,
            },
            "category_counts": dict(categories_count),
            "stations": station_assessments,
            "cycle_history": cycle_metrics
        }

        return self.analysis

    def _calc_duration_string(self, history, run_length):
        """Calculates readable duration string for the latest state run."""
        if not history or run_length <= 0:
            return "unknown"
        if run_length == 1:
            return "1 cycle (~30m)"

        try:
            streak_start_ts = history[-run_length][0]
            latest_ts = history[-1][0]
            dt_start = datetime.fromisoformat(streak_start_ts.replace('Z', '+00:00'))
            dt_end = datetime.fromisoformat(latest_ts.replace('Z', '+00:00'))
            diff_sec = int((dt_end - dt_start).total_seconds())
            hours = diff_sec // 3600
            mins = (diff_sec % 3600) // 60
            if hours > 0:
                return f"{hours}h {mins}m ({run_length} cycles)"
            return f"{mins}m ({run_length} cycles)"
        except Exception:
            return f"{run_length} cycles"

    def _calc_hours_between(self, ts1, ts2):
        """Calculates elapsed hours between two timestamps."""
        try:
            dt1 = datetime.fromisoformat(ts1.replace('Z', '+00:00'))
            dt2 = datetime.fromisoformat(ts2.replace('Z', '+00:00'))
            return round(abs((dt2 - dt1).total_seconds()) / 3600.0, 1)
        except Exception:
            return 0.0

    def generate_markdown_report(self, output_file="soh_summary_report.md", report_mode="custom", archive=False):
        """Generates a comprehensive executive Markdown summary report."""
        if not self.analysis:
            self.analyze()

        k = self.analysis["kpi"]
        w = self.analysis["window"]
        cc = self.analysis["category_counts"]
        stations = self.analysis["stations"]

        # Filter stations by category
        newly_down = [s for s in stations.values() if s["category"] == "NEWLY_DOWN"]
        flapping = [s for s in stations.values() if s["category"] == "FLAPPING"]
        delayed = [s for s in stations.values() if s["category"] == "DELAYED"]
        recovered = [s for s in stations.values() if s["category"] == "RECOVERED"]
        chronic = [s for s in stations.values() if s["category"] == "CHRONIC_OFFLINE"]

        now_utc = datetime.now(timezone.utc)
        lines = []

        if report_mode == "daily":
            lines.append("# 📡 Oklahoma Seismic Network: Daily State of Health (SOH) Briefing")
            lines.append("*Daily Operational Telemetry Assessment & Field Action Priorities*  ")
        elif report_mode == "weekly":
            lines.append("# 📡 Oklahoma Seismic Network: Weekly State of Health (SOH) Executive Digest")
            lines.append("*Weekly Array Availability, Telemetry SLA Performance & Chronic Outage Triage*  ")
        else:
            lines.append("# 📡 Oklahoma Seismic Network: Station Health Summary Report")
            lines.append("*Operational Telemetry Assessment & State of Health Analysis*  ")

        lines.append("")
        lines.append(f"**Generated:** {now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}  ")
        lines.append(f"**Audit Window:** `{w['first_timestamp']}` &rarr; `{w['latest_timestamp']}` ({w['duration_hours']} hours, {w['total_cycles']} audit cycles)")
        lines.append("")
        lines.append("---")
        lines.append("")

        # 1. Executive Summary KPIs
        lines.append("## 1. Network Telemetry Executive KPIs")
        lines.append("")
        lines.append("| Metric | Current Value | Window Baseline | Status |")
        lines.append("| :--- | :---: | :---: | :---: |")
        lines.append(f"| **Network Availability** | **`{k['current_availability_pct']}%`** | `{k['window_start_availability_pct']}%` | {'🟢 Nominal' if k['current_availability_pct'] >= 93 else '🟡 Degraded'} ({k['availability_change_pct']:+0.2f}%) |")
        lines.append(f"| **Monitored Stations** | `{k['total_stations']}` | `{k['total_stations']}` | Total network footprint |")
        lines.append(f"| **🟢 Online Stations** | **`{k['current_online']}`** | — | Latency $\\le 30\\,\\text{{s}}$ |")
        lines.append(f"| **🟡 Delayed Stations** | **`{k['current_delayed']}`** | — | Latency $> 30\\,\\text{{s}}$ |")
        lines.append(f"| **🔴 Offline Stations** | **`{k['current_offline']}`** | — | Communication silent |")
        lines.append("")

        # 2. Action Required / Priority Alerts
        lines.append("## 2. 🚨 Operational Attention & Priority Alerts")
        lines.append("")
        if not newly_down and not flapping and not delayed:
            lines.append("> [!NOTE]")
            lines.append("> No urgent dropouts or flapping stations detected in this window. Telemetry is stable.")
            lines.append("")
        else:
            if newly_down:
                lines.append("### 🔴 Newly Dropped Stations (Action Required)")
                lines.append("| Station | Site Location | Lat / Lon | Down Duration | Uptime % | Diagnosis |")
                lines.append("| :--- | :--- | :--- | :--- | :---: | :--- |")
                for s in newly_down:
                    coords = f"{s['latitude']}, {s['longitude']}" if s['latitude'] else "—"
                    lines.append(f"| **`{s['network']}.{s['station']}`** | {s['site_name']} | `{coords}` | **{s['duration_str']}** | {s['uptime_pct']}% | {s['diagnosis']} |")
                lines.append("")

            if flapping:
                lines.append("### 🔄 Intermittent & Flapping Stations (Unstable Telemetry)")
                lines.append("| Station | Site Location | Transitions | Uptime % | Current State | Diagnosis |")
                lines.append("| :--- | :--- | :---: | :---: | :---: | :--- |")
                for s in flapping:
                    cur_badge = "🔴 Offline" if s['current_status'] == 'OFFLINE' else "🟢 Online"
                    lines.append(f"| **`{s['network']}.{s['station']}`** | {s['site_name']} | **{s['transitions_count']}** | {s['uptime_pct']}% | {cur_badge} | {s['diagnosis']} |")
                lines.append("")

            if delayed:
                lines.append("### 🟡 Delayed Telemetry Stations (Packet Lag)")
                lines.append("| Station | Site Location | Current Latency | Max Latency | Diagnosis |")
                lines.append("| :--- | :--- | :---: | :---: | :--- |")
                for s in delayed:
                    cur_lat = f"{s['avg_latency_s']}s" if s['avg_latency_s'] is not None else "—"
                    max_lat = f"{s['max_latency_s']}s" if s['max_latency_s'] is not None else "—"
                    lines.append(f"| **`{s['network']}.{s['station']}`** | {s['site_name']} | `{cur_lat}` | `{max_lat}` | {s['diagnosis']} |")
                lines.append("")

        # 3. Recovered Stations
        if recovered:
            lines.append("## 3. 🟢 Recently Recovered Stations")
            lines.append("")
            lines.append("| Station | Site Location | Current Status | Uptime % | Recovery Details |")
            lines.append("| :--- | :--- | :---: | :---: | :--- |")
            for s in recovered:
                lines.append(f"| **`{s['network']}.{s['station']}`** | {s['site_name']} | 🟢 Online | {s['uptime_pct']}% | {s['diagnosis']} |")
            lines.append("")

        # 4. Chronic Offline Stations
        lines.append("## 4. ⚪ Chronic / Baseline Offline Stations")
        lines.append("")
        lines.append(f"The following **{len(chronic)} stations** remained persistently offline throughout all {w['total_cycles']} audit cycles (0% availability in window):")
        lines.append("")
        lines.append("| Station | Site Location | Coordinates | Recommended Field Action |")
        lines.append("| :--- | :--- | :--- | :--- |")
        for s in chronic:
            coords = f"{s['latitude']}, {s['longitude']}" if s['latitude'] else "—"
            lines.append(f"| `{s['network']}.{s['station']}` | {s['site_name']} | `{coords}` | Verify station solar power, digitizer communication, and cell modem. |")
        lines.append("")

        # 5. Recommended Actions
        lines.append("## 5. 🛠 Recommended Field & Operational Actions")
        lines.append("")
        action_idx = 1
        if newly_down:
            sta_list = ", ".join([f"`{s['network']}.{s['station']}`" for s in newly_down])
            lines.append(f"{action_idx}. **Immediate Inspection**: Dispatch telemetry inquiry or modem power cycle for active outage station(s): {sta_list}.")
            action_idx += 1
        if flapping:
            sta_list = ", ".join([f"`{s['network']}.{s['station']}`" for s in flapping])
            lines.append(f"{action_idx}. **Cellular Link Diagnostic**: Inspect carrier signal strength, cell modem buffer, and antenna alignment for intermittent station(s): {sta_list}.")
            action_idx += 1
        if chronic:
            lines.append(f"{action_idx}. **Scheduled Maintenance**: Prioritize site visits or remote hardware troubleshooting for long-term chronic stations ({len(chronic)} stations offline).")
            action_idx += 1
        lines.append(f"{action_idx}. **Routine Monitoring**: Telemetry audit routine continues every 30 minutes via automated cron workflow.")
        lines.append("")

        report_content = "\n".join(lines)
        if output_file:
            os.makedirs(os.path.dirname(os.path.abspath(output_file)), exist_ok=True)
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(report_content)

        # Optional historical archiving
        if archive:
            date_str = now_utc.strftime("%Y%m%d")
            week_str = now_utc.strftime("%Y_W%U")
            if report_mode == "daily":
                archive_dir = "reports/daily"
                archive_name = f"soh_daily_{date_str}.md"
            elif report_mode == "weekly":
                archive_dir = "reports/weekly"
                archive_name = f"soh_weekly_{week_str}.md"
            else:
                archive_dir = "reports/custom"
                archive_name = f"soh_custom_{now_utc.strftime('%Y%m%d_%H%M%S')}.md"

            os.makedirs(archive_dir, exist_ok=True)
            archive_path = os.path.join(archive_dir, archive_name)
            with open(archive_path, 'w', encoding='utf-8') as af:
                af.write(report_content)

        return report_content

    def print_cli_summary(self, report_mode="custom"):
        """Prints a clean, colorized terminal summary."""
        if not self.analysis:
            self.analyze()

        k = self.analysis["kpi"]
        w = self.analysis["window"]
        stations = self.analysis["stations"]

        newly_down = [s for s in stations.values() if s["category"] == "NEWLY_DOWN"]
        flapping = [s for s in stations.values() if s["category"] == "FLAPPING"]
        recovered = [s for s in stations.values() if s["category"] == "RECOVERED"]
        chronic = [s for s in stations.values() if s["category"] == "CHRONIC_OFFLINE"]

        BOLD = "\033[1m"
        GREEN = "\033[0;32m"
        YELLOW = "\033[1;33m"
        RED = "\033[0;31m"
        CYAN = "\033[0;36m"
        RESET = "\033[0m"

        title_suffix = ""
        if report_mode == "daily":
            title_suffix = " (DAILY BRIEFING - PAST 24H)"
        elif report_mode == "weekly":
            title_suffix = " (WEEKLY DIGEST - PAST 7 DAYS)"

        print(f"\n{BOLD}{CYAN}======================================================================{RESET}")
        print(f"{BOLD}{CYAN} 📡 OKLAHOMA SEISMIC NETWORK TELEMETRY SITUATION REPORT{title_suffix}{RESET}")
        print(f"{BOLD}{CYAN}======================================================================{RESET}")
        print(f" Audit Window : {w['first_timestamp']} -> {w['latest_timestamp']} ({w['duration_hours']}h, {w['total_cycles']} cycles)")
        print(f" Availability : {BOLD}{k['current_availability_pct']}%{RESET} ({k['current_online']} Online, {k['current_delayed']} Delayed, {k['current_offline']} Offline / {k['total_stations']} total)")
        print(f"{BOLD}{CYAN}----------------------------------------------------------------------{RESET}")

        if newly_down:
            print(f"{BOLD}{RED}🔴 NEWLY DOWN STATIONS ({len(newly_down)}):{RESET}")
            for s in newly_down:
                print(f"   • {BOLD}{s['network']}.{s['station']:<6}{RESET} : {s['site_name']:<28} | Down: {s['duration_str']} | Uptime: {s['uptime_pct']}%")
        else:
            print(f"{GREEN}✅ No newly dropped stations in this window.{RESET}")

        if flapping:
            print(f"\n{BOLD}{YELLOW}🔄 FLAPPING / UNSTABLE STATIONS ({len(flapping)}):{RESET}")
            for s in flapping:
                cur = f"{RED}Offline{RESET}" if s['current_status'] == 'OFFLINE' else f"{GREEN}Online{RESET}"
                print(f"   • {BOLD}{s['network']}.{s['station']:<6}{RESET} : {s['site_name']:<28} | State Changes: {s['transitions_count']} | Current: {cur}")

        if recovered:
            print(f"\n{BOLD}{GREEN}🟢 RECENTLY RECOVERED STATIONS ({len(recovered)}):{RESET}")
            for s in recovered:
                print(f"   • {BOLD}{s['network']}.{s['station']:<6}{RESET} : {s['site_name']:<28} | Operating for: {s['duration_str']}")

        print(f"\n{BOLD}⚪ CHRONIC OFFLINE STATIONS ({len(chronic)}):{RESET}")
        chronic_str = ", ".join([f"{s['network']}.{s['station']}" for s in chronic])
        print(f"   {chronic_str}")
        print(f"{BOLD}{CYAN}======================================================================{RESET}\n")

    def export_json(self, output_file="soh_summary_report.json"):
        """Exports the analysis structure as JSON."""
        if not self.analysis:
            self.analyze()
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(self.analysis, f, indent=2)


def main():
    parser = argparse.ArgumentParser(
        description="Oklahoma Seismic Network Station Health & Telemetry Analyzer."
    )
    parser.add_argument(
        "--daily", action="store_true",
        help="Generate 24-hour Daily SOH Briefing (default output: daily_soh_report.md)."
    )
    parser.add_argument(
        "--weekly", action="store_true",
        help="Generate 7-day Weekly SOH Executive Digest (default output: weekly_soh_report.md)."
    )
    parser.add_argument(
        "--no-archive", action="store_true",
        help="Disable automatic historical report archiving in reports/ directory."
    )
    parser.add_argument(
        "--monitor-log", default="monitor_log.csv",
        help="Path to monitor_log.csv telemetry audit log."
    )
    parser.add_argument(
        "--cron-log", default="cron.log",
        help="Path to cron.log output (used if monitor_log is not available)."
    )
    parser.add_argument(
        "--meta", default="ogs_stations.csv",
        help="Path to ogs_stations.csv inventory metadata."
    )
    parser.add_argument(
        "--hours", type=float, default=None,
        help="Analysis lookback window in hours (default: 24h for daily, 168h for weekly, or all available)."
    )
    parser.add_argument(
        "--runs", type=int, default=None,
        help="Analysis lookback window in number of audit cycles (default: all available)."
    )
    parser.add_argument(
        "--output", "-o", default=None,
        help="Destination path for Markdown summary report."
    )
    parser.add_argument(
        "--json", default=None,
        help="Optional destination path for JSON summary export."
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress CLI terminal output."
    )
    parser.add_argument(
        "--pdf", action="store_true", default=False,
        help="Generate executive PDF report alongside Markdown (default: False to conserve disk space)."
    )
    args = parser.parse_args()

    # Determine mode, lookback window, and default output file
    if args.daily:
        report_mode = "daily"
        if args.hours is None and args.runs is None:
            args.hours = 24.0
        if args.output is None:
            args.output = "daily_soh_report.md"
    elif args.weekly:
        report_mode = "weekly"
        if args.hours is None and args.runs is None:
            args.hours = 168.0  # 7 days
        if args.output is None:
            args.output = "weekly_soh_report.md"
    else:
        report_mode = "custom"
        if args.output is None:
            args.output = "soh_summary_report.md"

    analyzer = StationAnalyzer(meta_file=args.meta)

    loaded = False
    if os.path.isfile(args.monitor_log):
        try:
            loaded = analyzer.load_from_monitor_log(
                log_file=args.monitor_log, max_hours=args.hours, max_runs=args.runs
            )
        except Exception as e:
            sys.stderr.write(f"Notice: Failed to load monitor_log.csv ({e}), falling back to cron.log\n")

    if not loaded and os.path.isfile(args.cron_log):
        loaded = analyzer.load_from_cron_log(
            cron_file=args.cron_log, max_hours=args.hours, max_runs=args.runs
        )

    if not loaded:
        sys.stderr.write("Error: Could not load telemetry data from monitor_log.csv or cron.log.\n")
        sys.exit(1)

    analyzer.analyze()

    should_archive = not args.no_archive and (args.daily or args.weekly)

    if args.output:
        analyzer.generate_markdown_report(
            output_file=args.output, report_mode=report_mode, archive=should_archive
        )
        if not args.quiet:
            print(f"📄 Summary report saved to: {args.output}")
            if should_archive:
                print(f"🗄️  Archived copy preserved in: reports/{report_mode}/")

        # Convert to executive PDF only if explicitly requested
        if args.pdf:
            try:
                from generate_pdf_report import convert_md_to_pdf
                convert_md_to_pdf(args.output)
                if should_archive:
                    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
                    week_str = datetime.now(timezone.utc).strftime("%Y_W%U")
                    arch_name = f"soh_daily_{date_str}.md" if report_mode == "daily" else f"soh_weekly_{week_str}.md"
                    arch_path = os.path.join(f"reports/{report_mode}", arch_name)
                    if os.path.isfile(arch_path):
                        convert_md_to_pdf(arch_path)
            except Exception as e:
                if not args.quiet:
                    sys.stderr.write(f"Notice: PDF generation skipped ({e})\n")

    if args.json:
        analyzer.export_json(output_file=args.json)
        if not args.quiet:
            print(f"📊 JSON summary saved to: {args.json}")

    if not args.quiet:
        analyzer.print_cli_summary(report_mode=report_mode)


if __name__ == "__main__":
    main()
