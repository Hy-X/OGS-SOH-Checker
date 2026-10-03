# Agent Knowledge Base: Oklahoma Seismic Network State of Health & Stream Visualization

This document is the primary reference for AI agents working in this repository (`/home/hongyuxiao/Projects/SOH_Check`). It outlines the system architecture, domain rules, telemetry protocols, custom scripts, and critical technical gotchas.

---

## 1. Project Context & Objectives

- **Workspace Path**: `/home/hongyuxiao/Projects/SOH_Check`
- **Core Mission**: Build and maintain an operational **Oklahoma Seismic Network State of Health & Stream Visualization** platform, assessing real-time telemetry, packet latency, and continuous seismic waveforms.
- **Primary Target Networks**: Oklahoma Geological Survey (**OGS**) networks:
  - `OK`: Permanent regional seismic network stations.
  - `O2`: Temporary and special project seismic stations consolidated under OGS.
- **Upstream Data Centers**: EarthScope (IRIS DMC) via FDSNWS web services and the real-time SeedLink server (`rtserve.earthscope.org`).

---

## 2. Core SOH Telemetry Concepts & Thresholds

State of Health (SOH) in this system is assessed through telemetry packet latency, data flow continuity, and communication link status.

### Latency Calculation
$$\text{Latency} = \text{UTCDateTime}_{\text{current}} - \text{TraceStats}_{\text{endtime}}$$

### Health Status Classifications
| Status | Condition | Terminal / UI Tag | Meaning |
| :--- | :--- | :---: | :--- |
| **ONLINE** | $\text{Latency} \le 30.0\,\text{s}$ | 🟢 | Normal real-time streaming; data packets arriving promptly. |
| **DELAYED** | $\text{Latency} > 30.0\,\text{s}$ | 🟡 | Telemetry lag; cell modem or server buffer delay. Data is arriving but buffered. |
| **OFFLINE** | No packets in audit window | 🔴 | Communication dropped, power failure, or station silent. |

### Logging Convention (`monitor_log.csv`)
Audit records are written with the schema:
`Timestamp,Network,Station,Status,Latency_s`
- `Timestamp`: ISO 8601 UTC string (`%Y-%m-%dT%H:%M:%SZ`).
- `Latency_s`: Floating point seconds rounded to 2 decimal places (empty string for OFFLINE).

---

## 3. Telemetry Ingestion Protocols

### A. SeedLink Streaming (Real-Time Ingestion)
- **Host**: `rtserve.earthscope.org` (Port 18000 default)
- **Library**: `obspy.clients.seedlink.easyseedlink.create_client`
- **Mechanism**: Event-driven TCP connection. As miniSEED packets arrive, `on_data(trace)` callback is triggered immediately.
- **Priority Channels**:
  - `HHZ`: High broadband (typically 100 Hz)
  - `EHZ`: Extremely short period (typically 100 Hz)
  - `BHZ`: Broadband (typically 20–40 Hz)
  - `HNZ`: High-gain strong motion / accelerometer

### B. FDSNWS Bulk Waveform Polling (Array Health Auditing)
- **Host**: `Client("IRIS")` (EarthScope FDSNWS `dataselect`)
- **Method**: `client.get_waveforms_bulk(bulk_request)`
- **Query Window**: Typically last 30–60 seconds ($t_1 = \text{now} - 60$, $t_2 = \text{now}$) to keep payload sizes small while determining the freshest trace endtime.

---

## 4. Technical Gotchas & Critical Rules

1. **⚠️ FDSN Historical Epochs & Station Duplication**:
   - Calling `client.get_stations(network="OK,O2", level="station")` without a time constraint returns all historical operational epochs, producing multiple duplicate rows for the same station code.
   - **Rule**: Always pass `starttime=UTCDateTime()` to filter server-side for *currently active* stations unless historical analysis is explicitly requested.

2. **⚠️ Strict UTC Requirement**:
   - **Never** use local system time (`datetime.now()`) for computing packet latency or logging telemetry events. Always use `obspy.UTCDateTime()`.

3. **⚠️ FDSN Bulk Request Exceptions**:
   - `client.get_waveforms_bulk(...)` raises `obspy.clients.fdsn.header.FDSNNoDataException` when no data matches the requested window.
   - **Rule**: Always catch `FDSNNoDataException` and treat it as empty waveform data rather than a fatal crash.

4. **⚠️ Multiple Traces per Station**:
   - When pulling bulk waveforms, gaps or sample-rate differences can return multiple `Trace` objects for a single station.
   - **Rule**: Find the maximum `tr.stats.endtime` across all traces for that station before computing latency.

5. **⚠️ SeisBench Stream Length Constraint (Auxiliary/Downstream)**:
   - If integrating deep-learning phase picking (e.g. `sbm.PhaseNet`), the model requires $\ge 3001$ samples (30 seconds at 100 Hz).
   - Traces with fewer samples will be dropped with a warning. Always pad short streams before inference:
     ```python
     endtime = max([tr.stats.endtime for tr in stream]) + 1.0
     stream.trim(endtime=endtime, pad=True, fill_value=0)
     ```

6. **⚠️ Cross-Network Duplicate Stations (e.g. `O2.SAND` vs `OK.SAND`)**:
   - Stations transitioning between temporary (`O2`) and permanent (`OK`) networks can appear under both networks simultaneously in active station inventories.
   - **Rule**: SOH life signals and packet latency must be audited and classified based on the **station name**. If `OK.SAND` is streaming live data while `O2.SAND` is silent, the station `SAND` is ONLINE. All station inventory records are preserved in the Station Telemetry Roster and Geographic SOH Map, sharing the active station health status so operational stations are never falsely reported as offline.

---

## 5. File & Script Registry

| File | Purpose | Key Functions / Details |
| :--- | :--- | :--- |
| [run_workflow.sh](file:///home/hongyuxiao/Projects/SOH_Check/run_workflow.sh) | Master workflow orchestrator | Automated end-to-end Bash runner: validates station metadata, audits live telemetry, compiles the SOH HTML dashboard, and generates waveform stream visualizations. |
| [soh_panel.py](file:///home/hongyuxiao/Projects/SOH_Check/soh_panel.py) | Interactive SOH Dashboard & Panel | Generates self-contained HTML dashboard with interactive Leaflet map, KPI cards, and station roster. Also provides `--cli` summary and `--serve` local web preview. |
| [stream_visualizer.py](file:///home/hongyuxiao/Projects/SOH_Check/stream_visualizer.py) | Waveform stream visualizer | Multi-station stacked waveform plotting, bandpass filtering, and time-frequency spectrogram analysis from FDSN or local MiniSEED buffers. |
| [verify_streaming.py](file:///home/hongyuxiao/Projects/SOH_Check/verify_streaming.py) | Real-time SOH life-signal monitor | Bulk queries active stations over a 60s window, calculates latency, classifies health status, logs to `monitor_log.csv`. Exports JSON snapshots via `--json`. |
| [seedlink_monitor.py](file:///home/hongyuxiao/Projects/SOH_Check/seedlink_monitor.py) | Real-time SeedLink streaming client | Subscribes to 150+ stations on `rtserve.earthscope.org`, reporting instant packet delivery and latency. Supports trace buffering and `--record` to MiniSEED. |
| [query_ogs.py](file:///home/hongyuxiao/Projects/SOH_Check/query_ogs.py) | Python FDSN metadata query tool | Queries `OK,O2` stations from EarthScope; exports to CSV, JSON, StationXML. Handles active/all filtering. |
| [query_ogs.sh](file:///home/hongyuxiao/Projects/SOH_Check/query_ogs.sh) | Bash automation wrapper | CLI interface for `query_ogs.py`. Default output: CSV + StationXML. Preview formatted via `column`. |
| [ogs_stations.csv](file:///home/hongyuxiao/Projects/SOH_Check/ogs_stations.csv) | Current active station inventory | Columns: `Network,Station,Latitude,Longitude,Elevation_m,SiteName,StartTime,EndTime`. |
| [monitor_log.csv](file:///home/hongyuxiao/Projects/SOH_Check/monitor_log.csv) | Persistent SOH log | Historical record of every station status audit for uptime tracking and trend analysis. |
| [requirements.txt](file:///home/hongyuxiao/Projects/SOH_Check/requirements.txt) | Environment dependencies | Core libraries: ObsPy, Pandas, Matplotlib, Requests. |
| [legacy/](file:///home/hongyuxiao/Projects/SOH_Check/legacy/) | Exploratory legacy scripts | Preserved tests: `test_seisbench.py`, `seisbench_tutorial.py`. |
| [README.md](file:///home/hongyuxiao/Projects/SOH_Check/README.md) | User-facing documentation | Architecture, quickstart, system overview, and development roadmap. |

---

## 6. Architecture & Guidelines for Future Agent Tasks

### A. SOH Panel Dashboard
When building or extending the SOH Dashboard (e.g., using Streamlit, Dash, or FastAPI/React):
- **Metrics Summary**: Display real-time KPI cards: Total Active Stations, Online count, Delayed count, Offline count, and Network Availability %.
- **Map View**: Use `ogs_stations.csv` coordinates to render an interactive map with color-coded pins (🟢 Online, 🟡 Delayed, 🔴 Offline).
- **Map Basemaps**: Do NOT use Carto (requires API keys) or OpenStreetMap (unreliable/blocked). Always use reliable, keyless ESRI ArcGIS REST basemaps: ESRI Dark Canvas (`Canvas/World_Dark_Gray_Base`), ESRI Satellite (`World_Imagery`), ESRI Topo (`World_Topo_Map`), or ESRI Street Map (`World_Street_Map`).
- **Latency Distribution**: Render latency histograms or box plots highlighting slow telemetry channels.
- **Log Replay & Trends**: Read from `monitor_log.csv` to calculate 24h / 7d station uptime percentages and outage durations.

### B. Stream Waveform Visualizer
When building real-time waveform visualization:
- Maintain an in-memory ring buffer (e.g. `collections.deque` or `obspy.Stream`) per channel.
- Implement downsampling / decimation for UI rendering to prevent frontend lag when plotting 50–100 Hz streams.
- Include interactive channel controls: station selection, bandpass filtering (e.g. 1.0–10.0 Hz), amplitude normalization, and trace scaling.
