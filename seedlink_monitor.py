#!/usr/bin/env python3
"""
seedlink_monitor.py
===================
Real-time SeedLink streaming client for Oklahoma Seismic Network stations.
Connects to EarthScope SeedLink server (rtserve.earthscope.org), subscribes
to multi-station vertical channels, calculates arrival latency in real time,
and optionally buffers stream traces for live visualization.
"""

import csv
import sys
import argparse
from obspy.clients.seedlink.easyseedlink import create_client
from obspy import UTCDateTime, Stream


class SeedLinkStreamBuffer:
    """Manages an in-memory stream buffer for real-time visualization."""
    def __init__(self, max_traces=100, record_file=None):
        self.max_traces = max_traces
        self.record_file = record_file
        self.stream = Stream()
        self.packet_count = 0

    def add_trace(self, trace):
        self.packet_count += 1
        now = UTCDateTime()
        latency = float(now - trace.stats.endtime)
        
        status_tag = "🟢" if latency <= 30.0 else ("🟡" if latency <= 120.0 else "🔴")
        print(f"{status_tag} [LIVE] {trace.id:<18} | End: {trace.stats.endtime} | Latency: {latency:5.1f}s | Pkts: {self.packet_count}")
        
        if self.record_file:
            self.stream.append(trace)
            if len(self.stream) > self.max_traces:
                self.stream = self.stream[-self.max_traces:]
            # Periodically flush to disk
            if self.packet_count % 20 == 0:
                self.save_stream()

    def save_stream(self):
        if self.record_file and len(self.stream) > 0:
            try:
                self.stream.write(self.record_file, format="MSEED")
            except Exception as e:
                print(f"⚠️ Warning: Could not write buffer to {self.record_file}: {e}")


def load_stations_from_csv(csv_file, filter_stations=None):
    """Loads station list from CSV file, optionally filtered."""
    stations = []
    try:
        with open(csv_file, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                sta = row["Station"]
                net = row["Network"]
                if filter_stations is None or sta in filter_stations or f"{net}.{sta}" in filter_stations:
                    stations.append((net, sta))
    except FileNotFoundError:
        print(f"❌ Error: Could not find '{csv_file}'. Please run query_ogs.sh first.")
        sys.exit(1)
    return stations


def main():
    parser = argparse.ArgumentParser(description="Oklahoma Seismic Network real-time SeedLink streaming monitor.")
    parser.add_argument("--csv", default="ogs_stations.csv", help="Station CSV file (default: ogs_stations.csv)")
    parser.add_argument("--server", default="rtserve.earthscope.org", help="SeedLink server host")
    parser.add_argument("--port", type=int, default=18000, help="SeedLink server port (default: 18000)")
    parser.add_argument("--channels", default="HHZ,EHZ,BHZ,HNZ", help="Comma-separated channel selectors")
    parser.add_argument("--stations", help="Comma-separated station codes to filter (e.g., U32A,CAL1,CCO2)")
    parser.add_argument("--record", dest="record_file", help="Path to continuously save buffered traces to MiniSEED")
    parser.add_argument("--max-traces", type=int, default=200, help="Max traces to keep in buffer (default: 200)")
    args = parser.parse_args()

    filter_stas = [s.strip() for s in args.stations.split(",")] if args.stations else None
    channels = [c.strip() for c in args.channels.split(",")]

    stations = load_stations_from_csv(args.csv, filter_stations=filter_stas)
    if not stations:
        print("❌ No matching stations found.")
        sys.exit(1)

    print(f"=========================================================")
    print(f" 📡 OKLAHOMA SEISMIC NETWORK SEEDLINK STREAM INGESTION & SOH MONITOR")
    print(f" Server   : {args.server}:{args.port}")
    print(f" Channels : {', '.join(channels)}")
    print(f" Stations : {len(stations)} station(s) selected")
    if args.record_file:
        print(f" Buffer   : Recording to {args.record_file} (max {args.max_traces} traces)")
    print(f"=========================================================")

    buffer = SeedLinkStreamBuffer(max_traces=args.max_traces, record_file=args.record_file)

    def on_data_received(trace):
        buffer.add_trace(trace)

    server_address = f"{args.server}:{args.port}"
    print(f"🔗 Connecting to {server_address}...")
    try:
        client = create_client(server_address, on_data=on_data_received)
    except Exception as e:
        print(f"❌ Failed to connect to SeedLink server: {e}")
        sys.exit(1)

    # Subscribe to selected stations and channels
    for net, sta in stations:
        for chan in channels:
            client.select_stream(net, sta, chan)

    print(f"🟢 Listening for streaming packets... (Press Ctrl+C to stop)\n")
    try:
        client.run()
    except KeyboardInterrupt:
        print("\n🛑 Stopped listening.")
        if args.record_file:
            buffer.save_stream()
            print(f"💾 Buffer saved to {args.record_file}")


if __name__ == "__main__":
    main()
