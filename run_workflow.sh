#!/usr/bin/env bash
# ==============================================================================
# run_workflow.sh
# ==============================================================================
# Master orchestration script for the Oklahoma Seismic Network State of Health &
# Stream Visualization platform.
#
# Workflows orchestrated:
#   1. Station Inventory Verification & Harvesting (query_ogs.sh / query_ogs.py)
#   2. Real-Time Telemetry & Life-Signal Auditing (verify_streaming.py)
#   3. Interactive SOH HTML Dashboard Generation (soh_panel.py)
#   4. Multi-Station Waveform Stream Visualization (stream_visualizer.py)
#   5. Optional Live SeedLink Streaming or Local HTTP Dashboard Server
# ==============================================================================

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ------------------------------------------------------------------------------
# Terminal Colors & Styling
# ------------------------------------------------------------------------------
BOLD="\033[1m"
GREEN="\033[0;32m"
YELLOW="\033[1;33m"
CYAN="\033[0;36m"
RED="\033[0;31m"
BLUE="\033[0;34m"
RESET="\033[0m"

log_info()    { echo -e "${CYAN}ℹ️  [INFO]${RESET} $1"; }
log_success() { echo -e "${GREEN}✅ [SUCCESS]${RESET} $1"; }
log_warn()    { echo -e "${YELLOW}⚠️  [WARNING]${RESET} $1"; }
log_error()   { echo -e "${RED}❌ [ERROR]${RESET} $1"; }
log_header()  {
    echo -e "\n${BOLD}${BLUE}======================================================================${RESET}"
    echo -e "${BOLD}${BLUE} $1${RESET}"
    echo -e "${BOLD}${BLUE}======================================================================${RESET}"
}

# ------------------------------------------------------------------------------
# Auto-detect Python Environment with ObsPy
# ------------------------------------------------------------------------------
find_python() {
    if command -v python3 &>/dev/null && python3 -c "import obspy" &>/dev/null; then
        echo "python3"
    elif [ -n "$CONDA_PREFIX" ] && [ -x "$CONDA_PREFIX/bin/python" ] && "$CONDA_PREFIX/bin/python" -c "import obspy" &>/dev/null; then
        echo "$CONDA_PREFIX/bin/python"
    elif [ -x "/home/hongyuxiao/miniconda3/envs/TL/bin/python" ] && /home/hongyuxiao/miniconda3/envs/TL/bin/python -c "import obspy" &>/dev/null; then
        echo "/home/hongyuxiao/miniconda3/envs/TL/bin/python"
    else
        echo "python3"
    fi
}

PYTHON_BIN="$(find_python)"

# Verify Python dependencies
verify_environment() {
    if ! "$PYTHON_BIN" -c "import obspy, pandas, matplotlib" &>/dev/null; then
        log_error "Missing required Python packages in '$PYTHON_BIN'."
        echo "Please install dependencies via: pip install -r requirements.txt"
        exit 1
    fi
}

# ------------------------------------------------------------------------------
# Default Settings
# ------------------------------------------------------------------------------
STATION_CSV="ogs_stations.csv"
LOG_CSV="monitor_log.csv"
HTML_OUT="soh_dashboard.html"
IMAGE_OUT="waveform_stream.png"
WINDOW_SECONDS=60
SERVE_PORT=8050
MONITOR_INTERVAL=30
SAMPLE_STATIONS="OK.CHOK,OK.BLOK,OK.SMO"

# Flags
DO_HARVEST=false
DO_AUDIT=true
DO_DASHBOARD=true
DO_VISUALIZE=true
DO_SERVE=false
DO_STREAM=false
DO_LOOP=false
IS_CI=false

# Auto-detect non-interactive CI / cron environments
if [ "${CI:-false}" = "true" ] || [ "${GITHUB_ACTIONS:-false}" = "true" ] || ! [ -t 1 ]; then
    IS_CI=true
fi

# ------------------------------------------------------------------------------
# CLI Help Menu
# ------------------------------------------------------------------------------
show_help() {
    cat << EOF
Usage: $(basename "$0") [OPTIONS]

Master orchestrator for the Oklahoma Seismic Network State of Health & Stream Visualization workflow.

OPTIONS:
  -h, --help               Show this help menu and exit
  --all                    Execute full end-to-end workflow (Default)
  --refresh-meta           Force re-harvesting of active station inventory (query_ogs.sh)
  --audit-only             Run real-time SOH health audit only
  --dashboard-only         Generate HTML SOH dashboard only
  --visualize-only         Generate waveform stream plot only
  --serve [PORT]           Launch local web server for the dashboard (default port: 8050)
  --stream                 Start real-time SeedLink streaming packet monitor
  --monitor [INTERVAL]     Run continuous telemetry monitoring loop (interval in seconds, default: 30)
  --stations [LIST]        Comma-separated station list for waveform visualizer (default: $SAMPLE_STATIONS)
  --window [SECONDS]       FDSN audit lookback window in seconds (default: 60)

EXAMPLES:
  # 1. Run complete end-to-end pipeline (audit -> dashboard -> waveform plot)
  ./$(basename "$0")

  # 2. Run complete pipeline and immediately serve the web dashboard
  ./$(basename "$0") --serve

  # 3. Refresh station inventory from EarthScope before running audit
  ./$(basename "$0") --refresh-meta

  # 4. Run continuous monitoring loop every 30 seconds
  ./$(basename "$0") --monitor 30

  # 5. Visualize custom stations
  ./$(basename "$0") --visualize-only --stations "OK.U32A,OK.CCO2,O2.CAL1"
EOF
}

# ------------------------------------------------------------------------------
# Parse Command Line Arguments
# ------------------------------------------------------------------------------
while [[ "$#" -gt 0 ]]; do
    case "$1" in
        -h|--help)
            show_help
            exit 0
            ;;
        --all)
            DO_HARVEST=false
            DO_AUDIT=true
            DO_DASHBOARD=true
            DO_VISUALIZE=true
            ;;
        --refresh-meta)
            DO_HARVEST=true
            ;;
        --audit-only)
            DO_AUDIT=true
            DO_DASHBOARD=false
            DO_VISUALIZE=false
            ;;
        --dashboard-only)
            DO_AUDIT=false
            DO_DASHBOARD=true
            DO_VISUALIZE=false
            ;;
        --visualize-only)
            DO_AUDIT=false
            DO_DASHBOARD=false
            DO_VISUALIZE=true
            ;;
        --serve)
            DO_SERVE=true
            if [[ -n "$2" && "$2" =~ ^[0-9]+$ ]]; then
                SERVE_PORT="$2"
                shift
            fi
            ;;
        --stream)
            DO_STREAM=true
            ;;
        --monitor)
            DO_LOOP=true
            if [[ -n "$2" && "$2" =~ ^[0-9]+$ ]]; then
                MONITOR_INTERVAL="$2"
                shift
            fi
            ;;
        --stations)
            if [[ -n "$2" ]]; then
                SAMPLE_STATIONS="$2"
                shift
            fi
            ;;
        --window)
            if [[ -n "$2" && "$2" =~ ^[0-9]+$ ]]; then
                WINDOW_SECONDS="$2"
                shift
            fi
            ;;
        --ci|--cron)
            IS_CI=true
            DO_SERVE=false
            ;;
        *)
            log_error "Unknown option: $1"
            echo "Use --help for available options."
            exit 1
            ;;
    esac
    shift
done

# ------------------------------------------------------------------------------
# Concurrency Lock (EarthScope Good Citizenship: prevents overlapping cron runs)
# ------------------------------------------------------------------------------
LOCK_FILE="$SCRIPT_DIR/.workflow.lock"
exec 200>"$LOCK_FILE"
if command -v flock &>/dev/null; then
    if ! flock -n 200; then
        log_warn "Another workflow execution is currently running. Exiting cleanly to avoid overlapping service requests."
        exit 0
    fi
fi

# ------------------------------------------------------------------------------
# Pipeline Execution
# ------------------------------------------------------------------------------

verify_environment

echo -e "${BOLD}${CYAN}"
echo "   ___   ____ ____    ____   ___  _   _    ____ _               _    "
echo "  / _ \ / ___/ ___|  / ___| / _ \| | | |  / ___| |__   ___  ___| | __"
echo " | | | | |  _\___ \  \___ \| | | | |_| | | |   | '_ \ / _ \/ __| |/ /"
echo " | |_| | |_| |___) |  ___) | |_| |  _  | | |___| | | |  __/ (__|   < "
echo "  \___/ \____|____/  |____/ \___/|_| |_|  \____|_| |_|\___|\___|_|\_\ "
echo "  Oklahoma Seismic Network State of Health & Stream Visualization"
echo -e "${RESET}"
log_info "Using Python interpreter: ${BOLD}$PYTHON_BIN${RESET}"

# ------------------------------------------------------------------------------
# STEP 1: Station Inventory Validation
# ------------------------------------------------------------------------------
if [ "$DO_HARVEST" = true ] || [ ! -f "$STATION_CSV" ]; then
    log_header "STEP 1: Harvesting Active Station Inventory (FDSNWS)"
    log_info "Querying active stations for networks OK and O2..."
    ./query_ogs.sh csv json xml
    log_success "Station inventory ready: $STATION_CSV"
else
    log_info "Station inventory found: $STATION_CSV (skipping harvest; use --refresh-meta to force)"
fi

# ------------------------------------------------------------------------------
# Optional: Continuous Monitoring Loop Mode
# ------------------------------------------------------------------------------
if [ "$DO_LOOP" = true ]; then
    log_header "MONITORING MODE: Running Continuous SOH Telemetry Loop"
    log_info "Auditing every ${MONITOR_INTERVAL}s (Lookback window: ${WINDOW_SECONDS}s)"
    log_info "Press Ctrl+C to terminate."
    "$PYTHON_BIN" verify_streaming.py --csv "$STATION_CSV" --window "$WINDOW_SECONDS" --loop "$MONITOR_INTERVAL"
    exit 0
fi

# ------------------------------------------------------------------------------
# Optional: SeedLink Streaming Mode
# ------------------------------------------------------------------------------
if [ "$DO_STREAM" = true ]; then
    log_header "STREAMING MODE: Live SeedLink Packet Ingestion"
    log_info "Connecting to EarthScope SeedLink server (rtserve.earthscope.org)..."
    "$PYTHON_BIN" seedlink_monitor.py --csv "$STATION_CSV"
    exit 0
fi

# ------------------------------------------------------------------------------
# STEP 2: Real-time SOH Audit
# ------------------------------------------------------------------------------
if [ "$DO_AUDIT" = true ]; then
    log_header "STEP 2: Real-Time State of Health (SOH) Audit"
    log_info "Polling active station life signals (last ${WINDOW_SECONDS}s window)..."
    "$PYTHON_BIN" verify_streaming.py --csv "$STATION_CSV" --window "$WINDOW_SECONDS" --json "soh_status.json"
    log_success "Audit logged to $LOG_CSV and snapshot written to soh_status.json"
fi

# ------------------------------------------------------------------------------
# STEP 3: SOH Panel Dashboard Generation
# ------------------------------------------------------------------------------
if [ "$DO_DASHBOARD" = true ]; then
    log_header "STEP 3: Compiling Interactive SOH Panel Dashboard"
    log_info "Building standalone HTML dashboard with ESRI basemaps..."
    "$PYTHON_BIN" soh_panel.py --csv "$STATION_CSV" --log "$LOG_CSV" --html "$HTML_OUT"
    log_success "Dashboard successfully compiled: ${BOLD}$HTML_OUT${RESET}"
fi

# ------------------------------------------------------------------------------
# STEP 4: Stream Waveform Visualization
# ------------------------------------------------------------------------------
if [ "$DO_VISUALIZE" = true ]; then
    log_header "STEP 4: Real-Time Waveform Stream Visualization"
    log_info "Fetching live vertical traces for sample stations: $SAMPLE_STATIONS..."
    if "$PYTHON_BIN" stream_visualizer.py --stations "$SAMPLE_STATIONS" --duration 60 --filter 1.0,10.0 --output "$IMAGE_OUT"; then
        log_success "Waveform plot generated: ${BOLD}$IMAGE_OUT${RESET}"
    else
        log_warn "Waveform fetch completed with warnings (stations may be silent or lagging)."
    fi
fi

# ------------------------------------------------------------------------------
# Summary & Next Actions
# ------------------------------------------------------------------------------
log_header "WORKFLOW COMPLETE"
echo -e "  📄 ${BOLD}SOH Dashboard HTML${RESET} : file://$SCRIPT_DIR/$HTML_OUT"
if [ -f "$IMAGE_OUT" ]; then
    echo -e "  📈 ${BOLD}Waveform Stream PNG${RESET}: file://$SCRIPT_DIR/$IMAGE_OUT"
fi
echo -e "  📋 ${BOLD}Telemetry Audit Log${RESET}: file://$SCRIPT_DIR/$LOG_CSV"
echo ""

# Launch web server if requested
if [ "$DO_SERVE" = true ]; then
    log_info "Starting local web server on port $SERVE_PORT..."
    echo -e "👉 Open in your browser: ${BOLD}${CYAN}http://localhost:${SERVE_PORT}/$HTML_OUT${RESET}"
    echo "Press Ctrl+C to stop the server."
    "$PYTHON_BIN" -m http.server "$SERVE_PORT"
elif [ "$IS_CI" = false ]; then
    echo -e "💡 ${CYAN}Tip:${RESET} Run ${BOLD}./run_workflow.sh --serve${RESET} to view the interactive dashboard in your browser."
fi
