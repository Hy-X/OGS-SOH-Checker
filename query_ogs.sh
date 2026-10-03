#!/bin/bash
# query_ogs.sh
# Bash script to automate querying OGS seismic stations and outputting to CSV, XML, or JSON

FORMATS=()
FETCH_ALL=""

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        csv|xml|json) FORMATS+=("$1") ;;
        --all) FETCH_ALL="--all" ;;
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

# Default formats if none provided
if [ ${#FORMATS[@]} -eq 0 ]; then
    FORMATS=("csv" "xml")
fi

NETWORKS="OK,O2" # OK is the main OGS network, O2 is temporary OGS networks
PREFIX="ogs_stations"

echo "========================================================="
echo " Fetching Oklahoma Seismic Network (OGS) Station Data  "
echo " Networks: $NETWORKS"
echo " Formats : ${FORMATS[*]}"
if [ -n "$FETCH_ALL" ]; then
    echo " Filter  : All stations (including inactive)"
else
    echo " Filter  : Active stations only"
fi
echo "========================================================="

# Call the Python script to do the heavy lifting of querying FDSN
python3 query_ogs.py --network "$NETWORKS" --prefix "$PREFIX" --formats "${FORMATS[@]}" $FETCH_ALL

# Check if the Python script succeeded
if [ $? -eq 0 ]; then
    echo -e "\n✅ Successfully fetched station data!"
    
    # Provide a preview if CSV was generated
    if [[ " ${FORMATS[*]} " =~ " csv " ]]; then
        echo -e "\n📄 Preview of the CSV data:"
        column -s, -t < "${PREFIX}.csv" | head -n 8
        echo "..."
    fi
else
    echo -e "\n❌ Error occurred while fetching data."
    exit 1
fi
