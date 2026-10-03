#!/usr/bin/env python3
"""
stream_visualizer.py
====================
Oklahoma Seismic Network Waveform Stream Visualizer.
Fetches, processes, and visualizes real-time or recent seismic waveform streams
from EarthScope / OGS stations (via FDSNWS or local MiniSEED buffers).
Supports multi-station stacked waveforms, 3-component views, spectrograms,
and bandpass filtering.
"""

import os
import sys
import argparse
import warnings
from obspy import UTCDateTime, read, Stream
from obspy.clients.fdsn import Client
from obspy.clients.fdsn.header import FDSNNoDataException
import matplotlib
matplotlib.use('Agg')  # Headless backend for command-line rendering
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

warnings.filterwarnings("ignore")


def fetch_recent_waveforms(stations, channels="*Z", duration=120, client_name="IRIS"):
    """
    Fetches the most recent waveforms for the specified stations and channels.

    Parameters:
        stations (list of str): Station codes or 'NET.STA' strings.
        channels (str): Channel pattern (e.g., '*Z' or 'HHZ,EHZ,BHZ').
        duration (int): Duration in seconds to retrieve up to now.
        client_name (str): FDSN client name.

    Returns:
        obspy.Stream: Downloaded waveform stream.
    """
    user_agent = "Oklahoma-Seismic-Stream-Visualizer/1.0 (EarthScope Good Citizenship; Automated Telemetry Audit)"
    client = Client(client_name, timeout=30, user_agent=user_agent)
    now = UTCDateTime()
    t1 = now - duration
    t2 = now

    bulk_request = []
    chan_list = [c.strip() for c in channels.split(",")] if "," in channels else [channels]

    for item in stations:
        if "." in item:
            net, sta = item.split(".", 1)
        else:
            # Try OK network as default, or wildcard
            net = "OK"
            sta = item

        for ch in chan_list:
            bulk_request.append((net, sta, "*", ch, t1, t2))

    print(f"📡 Fetching waveforms for {len(bulk_request)} stream request(s) over the last {duration}s...")
    try:
        st = client.get_waveforms_bulk(bulk_request)
        return st
    except FDSNNoDataException:
        print("⚠️ No waveform data returned for the requested parameters.")
        return Stream()
    except Exception as e:
        print(f"❌ Failed to fetch waveforms: {e}")
        return Stream()


def process_stream(st, bandpass=None):
    """Applies detrending, tapering, and optional bandpass filtering."""
    if len(st) == 0:
        return st

    st_clean = st.copy()
    # Merge traces with identical IDs if continuous
    st_clean.merge(method=1, fill_value='interpolate')

    for tr in st_clean:
        tr.detrend("demean")
        tr.detrend("linear")
        tr.taper(max_percentage=0.05, type="cosine")
        if bandpass:
            low, high = bandpass
            tr.filter("bandpass", freqmin=low, freqmax=high, corners=4, zerophase=True)

    return st_clean


def plot_waveform_stack(st, output_file="waveform_stream.png", title="Oklahoma Seismic Network - Real-Time Waveforms", bandpass=None):
    """Plots a stacked multi-trace view of all traces in the stream."""
    if len(st) == 0:
        print("❌ Cannot plot: Stream is empty.")
        return False

    n_traces = len(st)
    fig, axes = plt.subplots(n_traces, 1, figsize=(14, max(4, n_traces * 1.6)), sharex=True)
    if n_traces == 1:
        axes = [axes]

    fig.patch.set_facecolor('#0f172a')
    filter_label = f" | Bandpass: {bandpass[0]}-{bandpass[1]} Hz" if bandpass else ""

    plt.suptitle(f"{title}{filter_label}", color="#f8fafc", fontsize=14, fontweight="bold", y=0.98)

    for idx, tr in enumerate(st):
        ax = axes[idx]
        ax.set_facecolor('#1e293b')

        # Convert times to matplotlib datetime numbers
        times = tr.times("matplotlib")
        data = tr.data

        # Color alternating or channel based
        color = "#38bdf8" if "Z" in tr.stats.channel else ("#34d399" if "N" in tr.stats.channel else "#f472b6")
        ax.plot(times, data, color=color, linewidth=0.8)

        # Labels
        label = f"{tr.id} ({tr.stats.sampling_rate} Hz)"
        ax.text(0.01, 0.82, label, transform=ax.transAxes, color="#f8fafc",
                fontsize=9, fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.2", facecolor="#334155", edgecolor="#475569", alpha=0.8))

        # Peak amplitude label
        max_amp = max(abs(data)) if len(data) > 0 else 0
        ax.text(0.99, 0.82, f"Peak: {max_amp:.1e}", transform=ax.transAxes, color="#94a3b8",
                fontsize=8, ha="right",
                bbox=dict(boxstyle="round,pad=0.2", facecolor="#1e293b", edgecolor="#475569", alpha=0.6))

        # Styling
        ax.tick_params(colors="#94a3b8", labelsize=8)
        ax.grid(True, linestyle="--", alpha=0.2, color="#94a3b8")
        for spine in ax.spines.values():
            spine.set_color('#475569')

    # Date formatting on x-axis
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S\n%Y-%m-%d', tz=mdates.UTC))
    axes[-1].set_xlabel("Time (UTC)", color="#94a3b8", fontsize=10)

    plt.tight_layout(rect=[0, 0.03, 1, 0.95])
    plt.savefig(output_file, dpi=150, facecolor=fig.get_facecolor(), edgecolor='none')
    plt.close()
    print(f"✅ Saved waveform visualization: {output_file}")
    return True


def plot_spectrogram(tr, output_file="spectrogram.png"):
    """Generates a high-resolution time-frequency spectrogram for a single trace."""
    fig = plt.figure(figsize=(12, 6))
    fig.patch.set_facecolor('#0f172a')

    # Plot spectrogram via ObsPy
    tr.spectrogram(log=True, title=f"Spectrogram: {tr.id}", axes=plt.gca(), cmap="viridis")

    plt.tight_layout()
    plt.savefig(output_file, dpi=150)
    plt.close()
    print(f"✅ Saved spectrogram: {output_file}")


def main():
    parser = argparse.ArgumentParser(description="Oklahoma Seismic Network Waveform Stream Visualizer")
    parser.add_argument("--stations", default="OK.U32A,OK.CCO2,O2.CAL1",
                        help="Comma-separated station list (e.g. OK.U32A,OK.CCO2 or U32A,CCO2)")
    parser.add_argument("--channels", default="*Z", help="Channel query (default: *Z)")
    parser.add_argument("--duration", type=int, default=120, help="Window duration in seconds (default: 120)")
    parser.add_argument("--filter", dest="bandpass", help="Bandpass filter low,high in Hz (e.g. 1.0,10.0)")
    parser.add_argument("--source", choices=["fdsn", "mseed"], default="fdsn",
                        help="Waveform source: 'fdsn' for live query, 'mseed' for local file")
    parser.add_argument("--file", help="Path to MiniSEED file (required if --source mseed)")
    parser.add_argument("--spectrogram", action="store_true", help="Plot spectrogram of the first trace")
    parser.add_argument("--output", default="waveform_stream.png", help="Output image file (default: waveform_stream.png)")
    args = parser.parse_args()

    # Parse filter if given
    bandpass = None
    if args.bandpass:
        try:
            parts = [float(x.strip()) for x in args.bandpass.split(",")]
            if len(parts) == 2:
                bandpass = (parts[0], parts[1])
        except ValueError:
            print("❌ Invalid bandpass format. Use low,high (e.g., 1.0,10.0)")
            sys.exit(1)

    # Ingest waveforms
    if args.source == "mseed":
        if not args.file or not os.path.isfile(args.file):
            print(f"❌ MiniSEED file not found: {args.file}")
            sys.exit(1)
        print(f"📂 Loading waveforms from {args.file}...")
        st = read(args.file)
    else:
        stations = [s.strip() for s in args.stations.split(",")]
        st = fetch_recent_waveforms(stations, channels=args.channels, duration=args.duration)

    if len(st) == 0:
        print("❌ No waveforms available to display.")
        sys.exit(1)

    print(f"📊 Processing {len(st)} waveform trace(s)...")
    st_proc = process_stream(st, bandpass=bandpass)

    if args.spectrogram:
        plot_spectrogram(st_proc[0], output_file=args.output)
    else:
        plot_waveform_stack(st_proc, output_file=args.output, bandpass=bandpass)


if __name__ == "__main__":
    main()
