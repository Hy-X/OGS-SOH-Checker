#!/usr/bin/env python3
"""
soh_panel.py
============
Oklahoma Seismic Network State of Health & Stream Visualization - Dashboard Generator.
Aggregates station inventory metadata (ogs_stations.csv), real-time life-signal
audits, and historical telemetry logs (monitor_log.csv).
Generates an interactive, standalone HTML SOH dashboard with geospatial maps,
telemetry latency distribution, and operational station rosters.
"""

import os
import csv
import sys
import json
import argparse
from http.server import HTTPServer, SimpleHTTPRequestHandler
import socketserver
import webbrowser

# Import audit function if available
try:
    from verify_streaming import audit_station_health
except ImportError:
    audit_station_health = None


def load_stations_metadata(csv_file="ogs_stations.csv"):
    """Loads station metadata from CSV into a lookup dict."""
    metadata = {}
    if not os.path.isfile(csv_file):
        return metadata

    with open(csv_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = f"{row.get('Network', '')}.{row.get('Station', '')}"
            try:
                lat = float(row.get("Latitude", 0.0))
                lon = float(row.get("Longitude", 0.0))
                elev = float(row.get("Elevation_m", 0.0))
            except ValueError:
                lat, lon, elev = 0.0, 0.0, 0.0

            metadata[key] = {
                "network": row.get("Network", ""),
                "station": row.get("Station", ""),
                "latitude": lat,
                "longitude": lon,
                "elevation": elev,
                "site_name": row.get("SiteName", "Unknown"),
                "start_time": row.get("StartTime", ""),
                "end_time": row.get("EndTime", "Active"),
            }
    return metadata


def load_latest_from_log(log_file="monitor_log.csv"):
    """Reads the most recent audit snapshot from monitor_log.csv."""
    if not os.path.isfile(log_file):
        return {}, None

    records = []
    with open(log_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            records.append(row)

    if not records:
        return {}, None

    # Find the latest timestamp in the log
    latest_ts = records[-1]["Timestamp"]

    latest_status = {}
    for r in reversed(records):
        if r["Timestamp"] != latest_ts:
            break
        key = f"{r['Network']}.{r['Station']}"
        lat_val = float(r["Latency_s"]) if r.get("Latency_s") else None
        latest_status[key] = {
            "network": r["Network"],
            "station": r["Station"],
            "status": r["Status"],
            "latency": lat_val
        }

    return latest_status, latest_ts


def compile_soh_dataset(stations_meta, latest_status, latest_ts):
    """
    Combines metadata with the latest health status.
    Health is resolved based on station name across duplicate networks:
    if a station (e.g. SAND) is streaming under any network (e.g. OK.SAND),
    its active health status is shared with all inventory instances of that station name.
    All station inventory entries are preserved for the Roster and Map.
    """
    # 1. Resolve best health status per station name
    station_health = {}
    for key, info in latest_status.items():
        sta = info["station"]
        st = info["status"]
        lat = info.get("latency")
        if sta not in station_health:
            station_health[sta] = info
        else:
            curr_st = station_health[sta]["status"]
            if curr_st == "OFFLINE" and st in ("ONLINE", "DELAYED"):
                station_health[sta] = info
            elif curr_st == "DELAYED" and st == "ONLINE":
                station_health[sta] = info
            elif curr_st == st and lat is not None:
                curr_lat = station_health[sta].get("latency")
                if curr_lat is None or lat < curr_lat:
                    station_health[sta] = info

    # 2. Build full station inventory roster and map entries (preserving all inventory stations)
    combined = []
    for key, meta in sorted(stations_meta.items()):
        sta = meta["station"]
        exact_info = latest_status.get(key)
        if exact_info and exact_info["status"] != "OFFLINE":
            status_info = exact_info
        elif sta in station_health:
            status_info = station_health[sta]
        else:
            status_info = {"status": "OFFLINE", "latency": None}

        status = status_info["status"]
        lat = status_info["latency"]

        combined.append({
            "id": key,
            "network": meta["network"],
            "station": meta["station"],
            "latitude": meta["latitude"],
            "longitude": meta["longitude"],
            "elevation": meta["elevation"],
            "site_name": meta["site_name"],
            "status": status,
            "latency": lat
        })

    # 3. Calculate summary metrics based on unique station names (matching life-signal monitor)
    unique_stations = {}
    for entry in combined:
        sta = entry["station"]
        st = entry["status"]
        if sta not in unique_stations:
            unique_stations[sta] = st
        elif unique_stations[sta] == "OFFLINE" and st in ("ONLINE", "DELAYED"):
            unique_stations[sta] = st
        elif unique_stations[sta] == "DELAYED" and st == "ONLINE":
            unique_stations[sta] = st

    counts = {
        "total": len(unique_stations),
        "online": sum(1 for s in unique_stations.values() if s == "ONLINE"),
        "delayed": sum(1 for s in unique_stations.values() if s == "DELAYED"),
        "offline": sum(1 for s in unique_stations.values() if s == "OFFLINE"),
    }
    active_count = counts["online"] + counts["delayed"]
    availability = round((active_count / counts["total"] * 100), 1) if counts["total"] > 0 else 0.0

    return {
        "timestamp": latest_ts or "No Audit Log Found",
        "counts": counts,
        "availability_pct": availability,
        "stations": combined
    }


def generate_html_dashboard(data, output_file="soh_dashboard.html"):
    """Generates a self-contained, interactive HTML SOH Dashboard."""
    json_data = json.dumps(data)

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Oklahoma Seismic Network State of Health & Stream Visualization</title>
    <!-- Leaflet CSS & JS for Interactive Map -->
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin=""/>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
    <style>
        :root {{
            --bg-primary: #0f172a;
            --bg-secondary: #1e293b;
            --bg-card: #334155;
            --text-primary: #f8fafc;
            --text-secondary: #94a3b8;
            --accent-green: #22c55e;
            --accent-amber: #f59e0b;
            --accent-red: #ef4444;
            --accent-blue: #38bdf8;
            --border: #475569;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }}
        body {{ background-color: var(--bg-primary); color: var(--text-primary); padding: 20px; }}
        .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--border); }}
        .title h1 {{ font-size: 1.6rem; font-weight: 700; color: #fff; }}
        .title p {{ color: var(--text-secondary); font-size: 0.9rem; margin-top: 4px; }}
        .badge-live {{ background: rgba(34, 197, 94, 0.2); color: var(--accent-green); border: 1px solid var(--accent-green); padding: 4px 10px; border-radius: 9999px; font-size: 0.8rem; font-weight: 600; display: inline-flex; align-items: center; gap: 6px; }}
        .badge-live::before {{ content: ""; width: 8px; height: 8px; background: var(--accent-green); border-radius: 50%; display: inline-block; box-shadow: 0 0 8px var(--accent-green); }}

        /* KPI Cards Grid */
        .kpi-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }}
        .kpi-card {{ background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 10px; padding: 18px; display: flex; flex-direction: column; }}
        .kpi-label {{ font-size: 0.85rem; color: var(--text-secondary); text-transform: uppercase; font-weight: 600; letter-spacing: 0.05em; }}
        .kpi-val {{ font-size: 2rem; font-weight: 700; margin-top: 8px; }}
        .kpi-card.online .kpi-val {{ color: var(--accent-green); }}
        .kpi-card.delayed .kpi-val {{ color: var(--accent-amber); }}
        .kpi-card.offline .kpi-val {{ color: var(--accent-red); }}
        .kpi-card.avail .kpi-val {{ color: var(--accent-blue); }}

        /* Main Workspace: Map & Table Layout */
        .grid-2col {{ display: grid; grid-template-columns: 1.2fr 1fr; gap: 20px; margin-bottom: 24px; }}
        @media (max-width: 1024px) {{ .grid-2col {{ grid-template-columns: 1fr; }} }}

        .panel-box {{ background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 10px; padding: 16px; overflow: hidden; display: flex; flex-direction: column; }}
        .panel-header {{ font-size: 1.1rem; font-weight: 600; margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center; }}
        #map {{ height: 500px; width: 100%; border-radius: 8px; }}

        /* Filters and Table */
        .table-controls {{ display: flex; gap: 10px; margin-bottom: 12px; }}
        .search-box {{ flex: 1; padding: 8px 12px; background: var(--bg-primary); border: 1px solid var(--border); border-radius: 6px; color: #fff; outline: none; }}
        .filter-btn {{ padding: 8px 14px; background: var(--bg-card); border: 1px solid var(--border); color: #fff; border-radius: 6px; cursor: pointer; font-size: 0.85rem; font-weight: 600; }}
        .filter-btn.active {{ background: var(--accent-blue); color: #000; border-color: var(--accent-blue); }}
        .table-wrap {{ max-height: 440px; overflow-y: auto; }}
        table {{ width: 100%; border-collapse: collapse; text-align: left; font-size: 0.85rem; }}
        th {{ background: var(--bg-primary); position: sticky; top: 0; padding: 10px; color: var(--text-secondary); border-bottom: 1px solid var(--border); }}
        td {{ padding: 10px; border-bottom: 1px solid rgba(255,255,255,0.05); }}
        tr:hover td {{ background: rgba(255,255,255,0.03); }}
        .status-pill {{ padding: 2px 8px; border-radius: 4px; font-weight: 600; font-size: 0.75rem; display: inline-block; }}
        .status-pill.ONLINE {{ background: rgba(34, 197, 94, 0.2); color: var(--accent-green); }}
        .status-pill.DELAYED {{ background: rgba(245, 158, 11, 0.2); color: var(--accent-amber); }}
        .status-pill.OFFLINE {{ background: rgba(239, 68, 68, 0.2); color: var(--accent-red); }}

        /* Custom dark theme styling for Leaflet controls */
        .leaflet-control-layers {{
            background: var(--bg-secondary) !important;
            color: var(--text-primary) !important;
            border: 1px solid var(--border) !important;
            border-radius: 8px !important;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4) !important;
            padding: 8px 12px !important;
            font-size: 0.85rem !important;
        }}
        .leaflet-control-layers-expanded {{
            background: var(--bg-secondary) !important;
            color: var(--text-primary) !important;
        }}
        .leaflet-control-layers label {{
            margin: 4px 0 !important;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        .leaflet-bar a {{
            background-color: var(--bg-secondary) !important;
            color: var(--text-primary) !important;
            border-bottom: 1px solid var(--border) !important;
        }}
        .leaflet-bar a:hover {{
            background-color: var(--bg-card) !important;
        }}
    </style>
</head>
<body>

    <div class="header">
        <div class="title">
            <h1>Oklahoma Seismic Network State of Health & Stream Visualization</h1>
            <p>Real-Time Network Telemetry, Station Latency & Health Classification</p>
        </div>
        <div>
            <span class="badge-live">Live UTC Audit: {data["timestamp"]}</span>
        </div>
    </div>

    <!-- KPI Summary Row -->
    <div class="kpi-grid">
        <div class="kpi-card">
            <span class="kpi-label">Monitored Stations</span>
            <span class="kpi-val">{data["counts"]["total"]}</span>
        </div>
        <div class="kpi-card online">
            <span class="kpi-label">Online (&le;30s)</span>
            <span class="kpi-val">{data["counts"]["online"]}</span>
        </div>
        <div class="kpi-card delayed">
            <span class="kpi-label">Delayed (&gt;30s)</span>
            <span class="kpi-val">{data["counts"]["delayed"]}</span>
        </div>
        <div class="kpi-card offline">
            <span class="kpi-label">Offline / Silent</span>
            <span class="kpi-val">{data["counts"]["offline"]}</span>
        </div>
        <div class="kpi-card avail">
            <span class="kpi-label">Network Availability</span>
            <span class="kpi-val">{data["availability_pct"]}%</span>
        </div>
    </div>

    <!-- Map & Table Row -->
    <div class="grid-2col">
        <div class="panel-box">
            <div class="panel-header">
                <span>🗺️ Geographic Station SOH Map</span>
                <span style="font-size:0.8rem; color:var(--text-secondary);">Click marker for station details</span>
            </div>
            <div id="map"></div>
        </div>

        <div class="panel-box">
            <div class="panel-header">
                <span>📋 Station Telemetry Roster</span>
            </div>
            <div class="table-controls">
                <input type="text" id="searchInput" class="search-box" placeholder="Search station, network, or site..." oninput="filterTable()">
                <button class="filter-btn active" onclick="setFilter('ALL', this)">All</button>
                <button class="filter-btn" onclick="setFilter('ONLINE', this)">Online</button>
                <button class="filter-btn" onclick="setFilter('DELAYED', this)">Delayed</button>
                <button class="filter-btn" onclick="setFilter('OFFLINE', this)">Offline</button>
            </div>
            <div class="table-wrap">
                <table id="stationTable">
                    <thead>
                        <tr>
                            <th>Station</th>
                            <th>Status</th>
                            <th>Latency</th>
                            <th>Site Location</th>
                        </tr>
                    </thead>
                    <tbody id="tableBody"></tbody>
                </table>
            </div>
        </div>
    </div>

    <script>
        const dataset = {json_data};
        let currentFilter = 'ALL';
        let searchQuery = '';

        // 1. ESRI Dark Gray Canvas (Base + Labels) - No API Key Needed
        const esriDarkBase = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{{z}}/{{y}}/{{x}}', {{
            attribution: 'Tiles &copy; Esri &mdash; Esri, DeLorme, NAVTEQ',
            maxZoom: 16
        }});
        const esriDarkRef = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{{z}}/{{y}}/{{x}}', {{
            maxZoom: 16
        }});
        const darkCanvas = L.layerGroup([esriDarkBase, esriDarkRef]);

        // 2. ESRI World Imagery (Satellite) - No API Key Needed
        const esriSatellite = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}', {{
            attribution: 'Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics',
            maxZoom: 18
        }});

        // 3. ESRI World Topo Map - No API Key Needed
        const esriTopo = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{{z}}/{{y}}/{{x}}', {{
            attribution: 'Tiles &copy; Esri &mdash; USGS, Esri',
            maxZoom: 18
        }});

        // 4. ESRI World Street Map - No API Key Needed
        const esriStreet = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{{z}}/{{y}}/{{x}}', {{
            attribution: 'Tiles &copy; Esri &mdash; Sources: Esri, DeLorme, NAVTEQ, USGS, Intermap',
            maxZoom: 18
        }});

        // Initialize Leaflet Map centered on Oklahoma with Dark Canvas default
        const map = L.map('map', {{
            center: [35.5, -97.5],
            zoom: 7,
            layers: [darkCanvas]
        }});

        // Add Layer Switcher Control (allows user to easily switch basemaps)
        const baseMaps = {{
            "🌌 Dark Canvas (Esri)": darkCanvas,
            "🛰️ Satellite Imagery (Esri)": esriSatellite,
            "⛰️ Topographic (Esri)": esriTopo,
            "🗺️ Street Map (Esri)": esriStreet
        }};
        L.control.layers(baseMaps, null, {{ position: 'topright' }}).addTo(map);

        const markers = [];

        function getMarkerColor(status) {{
            if (status === 'ONLINE') return '#22c55e';
            if (status === 'DELAYED') return '#f59e0b';
            return '#ef4444';
        }}

        // Render Map Markers
        dataset.stations.forEach(sta => {{
            if (sta.latitude && sta.longitude) {{
                const color = getMarkerColor(sta.status);
                const marker = L.circleMarker([sta.latitude, sta.longitude], {{
                    radius: 7,
                    fillColor: color,
                    color: '#ffffff',
                    weight: 1,
                    opacity: 0.9,
                    fillOpacity: 0.85
                }}).addTo(map);

                const latencyText = sta.latency !== null ? `${{sta.latency.toFixed(1)}}s` : 'No Data';
                marker.bindPopup(`
                    <div style="color:#000; font-size:13px; line-height:1.4;">
                        <strong>${{sta.network}}.${{sta.station}}</strong><br>
                        <em>${{sta.site_name}}</em><br>
                        <strong>Status:</strong> ${{sta.status}}<br>
                        <strong>Latency:</strong> ${{latencyText}}<br>
                        <strong>Coords:</strong> ${{sta.latitude.toFixed(4)}}, ${{sta.longitude.toFixed(4)}}<br>
                        <strong>Elevation:</strong> ${{sta.elevation}} m
                    </div>
                `);
                markers.push({{ id: sta.id, marker: marker, data: sta }});
            }}
        }});

        // Render Table Rows
        function renderTable() {{
            const tbody = document.getElementById('tableBody');
            tbody.innerHTML = '';

            const filtered = dataset.stations.filter(s => {{
                const matchesFilter = currentFilter === 'ALL' || s.status === currentFilter;
                const searchTarget = `${{s.network}} ${{s.station}} ${{s.site_name}}`.toLowerCase();
                const matchesSearch = searchTarget.includes(searchQuery.toLowerCase());
                return matchesFilter && matchesSearch;
            }});

            filtered.forEach(s => {{
                const tr = document.createElement('tr');
                const latText = s.latency !== null ? `${{s.latency.toFixed(1)}}s` : '-';
                tr.innerHTML = `
                    <td><strong>${{s.network}}.${{s.station}}</strong></td>
                    <td><span class="status-pill ${{s.status}}">${{s.status}}</span></td>
                    <td>${{latText}}</td>
                    <td style="color:var(--text-secondary);">${{s.site_name}}</td>
                `;
                tr.onclick = () => {{
                    const mObj = markers.find(m => m.id === s.id);
                    if (mObj) {{
                        map.flyTo([s.latitude, s.longitude], 10);
                        mObj.marker.openPopup();
                    }}
                }};
                tbody.appendChild(tr);
            }});
        }}

        function setFilter(filter, btn) {{
            currentFilter = filter;
            document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            renderTable();
        }}

        function filterTable() {{
            searchQuery = document.getElementById('searchInput').value;
            renderTable();
        }}

        renderTable();
    </script>
</body>
</html>
"""
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html_content)
    print(f"✅ Generated SOH Dashboard: {output_file}")


def print_cli_summary(data):
    """Outputs a clean summary to the console."""
    counts = data["counts"]
    print("=========================================================")
    print(" 📡 OKLAHOMA SEISMIC NETWORK STATE OF HEALTH (SOH) SUMMARY")
    print(f" Timestamp: {data['timestamp']}")
    print("=========================================================")
    print(f" Total Monitored Stations : {counts['total']}")
    print(f" 🟢 Online  (<=30s)       : {counts['online']}")
    print(f" 🟡 Delayed (>30s)        : {counts['delayed']}")
    print(f" 🔴 Offline (No data)     : {counts['offline']}")
    print(f" 📈 Network Availability  : {data['availability_pct']}%")
    print("=========================================================")


def main():
    parser = argparse.ArgumentParser(description="Oklahoma Seismic Network State of Health & Stream Visualization - Dashboard Generator")
    parser.add_argument("--csv", default="ogs_stations.csv", help="Station metadata CSV (default: ogs_stations.csv)")
    parser.add_argument("--log", default="monitor_log.csv", help="Monitor log file (default: monitor_log.csv)")
    parser.add_argument("--audit", action="store_true", help="Trigger a live FDSN health audit before generating")
    parser.add_argument("--html", default="soh_dashboard.html", help="HTML dashboard output file")
    parser.add_argument("--cli", action="store_true", help="Display CLI summary only")
    parser.add_argument("--serve", action="store_true", help="Launch a local HTTP server to view the dashboard")
    parser.add_argument("--port", type=int, default=8050, help="Port for local server (default: 8050)")
    args = parser.parse_args()

    # Load station metadata
    meta = load_stations_metadata(args.csv)
    if not meta:
        print(f"❌ Error: No station metadata found in '{args.csv}'. Run query_ogs.sh first.")
        sys.exit(1)

    latest_status = {}
    latest_ts = None

    if args.audit:
        if audit_station_health is None:
            print("❌ Cannot run live audit: verify_streaming module not available.")
            sys.exit(1)
        print("📡 Performing fresh live FDSN health audit...")
        result, online, delayed, offline, now = audit_station_health(
            csv_file=args.csv,
            log_file=args.log,
            write_log=True
        )
        latest_ts = result["timestamp"]
        if "inventory" in result:
            for item in result["inventory"]:
                latest_status[f"{item['network']}.{item['station']}"] = {
                    "network": item["network"], "station": item["station"],
                    "status": item["status"], "latency": item["latency_s"]
                }
        else:
            for o in result["online"]:
                latest_status[f"{o['network']}.{o['station']}"] = {
                    "network": o["network"], "station": o["station"],
                    "status": "ONLINE", "latency": o["latency_s"]
                }
            for d in result["delayed"]:
                latest_status[f"{d['network']}.{d['station']}"] = {
                    "network": d["network"], "station": d["station"],
                    "status": "DELAYED", "latency": d["latency_s"]
                }
            for off in result["offline"]:
                latest_status[f"{off['network']}.{off['station']}"] = {
                    "network": off["network"], "station": off["station"],
                    "status": "OFFLINE", "latency": None
                }
    else:
        # Load from log
        latest_status, latest_ts = load_latest_from_log(args.log)

    dataset = compile_soh_dataset(meta, latest_status, latest_ts)

    if args.cli:
        print_cli_summary(dataset)
        return

    generate_html_dashboard(dataset, output_file=args.html)
    print_cli_summary(dataset)

    if args.serve:
        os.chdir(os.path.dirname(os.path.abspath(args.html)) or ".")
        handler = SimpleHTTPRequestHandler
        print(f"\n🚀 Serving SOH Dashboard at http://localhost:{args.port}/{os.path.basename(args.html)}")
        print("Press Ctrl+C to terminate the server.")
        with socketserver.TCPServer(("", args.port), handler) as httpd:
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\n🛑 Server stopped.")


if __name__ == "__main__":
    main()
