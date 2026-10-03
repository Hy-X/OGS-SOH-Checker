#!/usr/bin/env python3
"""
query_ogs.py
============
FDSN station metadata harvester for Oklahoma Seismic Network (OGS).
Queries EarthScope (IRIS DMC) for network metadata and exports formatted
CSV, JSON, and StationXML inventories.
"""

import argparse
import csv
import json
import sys
from obspy.clients.fdsn import Client
from obspy.clients.fdsn.header import FDSNNoDataException
from obspy import UTCDateTime

def fetch_and_save_stations(networks, prefix, formats, fetch_all):
    user_agent = "Oklahoma-Seismic-Station-Harvester/1.0 (EarthScope Good Citizenship)"
    client = Client("IRIS", timeout=30, user_agent=user_agent)
    kwargs = {"network": networks, "level": "station"}
    now = UTCDateTime()
    
    # Filter active stations at the server level if --all is not set
    if not fetch_all:
        kwargs["starttime"] = now
        
    print(f"📡 Querying IRIS FDSNWS for networks: {networks}...")
    try:
        inventory = client.get_stations(**kwargs)
    except FDSNNoDataException:
        print("❌ No data found for the specified networks.")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Failed to fetch data: {e}")
        sys.exit(1)
    
    # Save StationXML natively via obspy if requested
    if "xml" in formats:
        xml_file = f"{prefix}.xml"
        inventory.write(xml_file, format="STATIONXML")
        print(f"💾 Saved StationXML: {xml_file}")

    # If csv or json is requested, extract flattened metadata
    if "csv" in formats or "json" in formats:
        stations_data = []
        for network in inventory:
            for station in network:
                stations_data.append({
                    "Network": network.code,
                    "Station": station.code,
                    "Latitude": station.latitude,
                    "Longitude": station.longitude,
                    "Elevation_m": station.elevation,
                    "SiteName": station.site.name if station.site else "Unknown",
                    "StartTime": str(station.start_date.datetime) if station.start_date else "",
                    "EndTime": str(station.end_date.datetime) if station.end_date else "Active"
                })
        
        if not stations_data:
            print("No station records extracted.")
            sys.exit(1)
            
        if "csv" in formats:
            csv_file = f"{prefix}.csv"
            with open(csv_file, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=stations_data[0].keys())
                writer.writeheader()
                writer.writerows(stations_data)
            print(f"💾 Saved CSV: {csv_file}")
            
        if "json" in formats:
            json_file = f"{prefix}.json"
            with open(json_file, 'w', encoding='utf-8') as f:
                json.dump(stations_data, f, indent=4)
            print(f"💾 Saved JSON: {json_file}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Oklahoma Seismic Network FDSN station metadata harvester.")
    parser.add_argument("--network", required=True, help="Network code(s) (e.g., OK,O2)")
    parser.add_argument("--prefix", required=True, help="Output file prefix (e.g., ogs_stations)")
    parser.add_argument("--formats", nargs="+", required=True, help="Output formats (e.g., csv xml json)")
    parser.add_argument("--all", action="store_true", help="Include inactive stations (default is active only)")
    args = parser.parse_args()
    
    fetch_and_save_stations(args.network, args.prefix, [f.lower() for f in args.formats], args.all)
