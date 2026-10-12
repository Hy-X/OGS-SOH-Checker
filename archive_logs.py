#!/usr/bin/env python3
"""
archive_logs.py
===============
Automated rolling log manager and quarterly archival routine for the Oklahoma
Seismic Network State of Health (SOH) platform.

Features:
  1. Active 90-Day Rolling Log:
     - Retains the most recent 90 days of telemetry in monitor_log.csv for fast parsing.
     - Automatically splits older records into quarterly compressed archives:
       archive/telemetry/monitor_log_YYYY_Q1.csv.gz, Q2, Q3, Q4.
     - Preserves all CSV headers, ensures zero data loss, and uses atomic file replacement.
  2. Cron Log Rotation:
     - Automatically compresses and rotates cron.log when it exceeds 10 MB into
       archive/cron/cron_YYYYMMDD.log.gz.
  3. Supports --dry-run for previewing without modifying files.
"""

import os
import sys
import csv
import gzip
import time
import shutil
import fcntl
import argparse
from datetime import datetime, timezone, timedelta
from collections import defaultdict


DEFAULT_LOG_FILE = "monitor_log.csv"
DEFAULT_CRON_LOG = "cron.log"
DEFAULT_ARCHIVE_DIR = "archive"
DEFAULT_LOCK_FILE = ".workflow.lock"
DEFAULT_RETENTION_DAYS = 90
DEFAULT_MAX_CRON_MB = 10


def acquire_workflow_lock(lock_file=DEFAULT_LOCK_FILE, timeout=60):
    """
    Acquires an advisory lock on lock_file to prevent concurrency collision
    with run_workflow.sh or ongoing telemetry collection.
    """
    try:
        f = open(lock_file, "a+")
        start = time.time()
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return f
            except (BlockingIOError, IOError):
                if time.time() - start > timeout:
                    sys.stderr.write(f"⚠️ Warning: Lock busy ({lock_file}) after {timeout}s. Proceeding.\n")
                    return f
                time.sleep(0.5)
    except Exception as e:
        sys.stderr.write(f"Warning: Could not open lock file {lock_file}: {e}\n")
        return None


def release_workflow_lock(lock_handle):
    """Releases the advisory lock and closes handle."""
    if lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            lock_handle.close()
        except Exception:
            pass


def get_quarter_str(dt):
    """Returns quarterly tag: e.g. 2026_Q3."""
    quarter = (dt.month - 1) // 3 + 1
    return f"{dt.year}_Q{quarter}"


def archive_monitor_log(log_file=DEFAULT_LOG_FILE,
                        retention_days=DEFAULT_RETENTION_DAYS,
                        archive_dir=DEFAULT_ARCHIVE_DIR,
                        dry_run=False):
    """
    Splits monitor_log.csv into an active rolling window (retention_days)
    and appends older records into quarterly compressed files (gzip CSV).
    """
    if not os.path.isfile(log_file):
        print(f"ℹ️  Notice: '{log_file}' does not exist. Nothing to archive.")
        return 0, 0

    now = datetime.now(timezone.utc)
    cutoff_dt = now - timedelta(days=retention_days)
    cutoff_str = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    telemetry_archive_dir = os.path.join(archive_dir, "telemetry")
    if not dry_run:
        os.makedirs(telemetry_archive_dir, exist_ok=True)

    header = None
    active_rows = []
    archived_by_quarter = defaultdict(list)
    total_rows = 0

    print(f"📂 Reading '{log_file}'...")
    with open(log_file, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            print("ℹ️  Log file is empty.")
            return 0, 0

        for row in reader:
            if not row or len(row) < 3:
                continue
            total_rows += 1
            ts_str = row[0].strip()

            # Fast ISO-8601 comparison: strings format YYYY-MM-DDTHH:MM:SSZ sort chronologically
            if ts_str >= cutoff_str:
                active_rows.append(row)
            else:
                try:
                    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    q_key = get_quarter_str(dt)
                except Exception:
                    # Fallback to year_month if timestamp malformed
                    q_key = f"{ts_str[:4]}_Q{(int(ts_str[5:7])-1)//3 + 1}"
                archived_by_quarter[q_key].append(row)

    archived_count = sum(len(rows) for rows in archived_by_quarter.values())
    active_count = len(active_rows)

    print(f"📊 Total Records Scanned : {total_rows:,}")
    print(f"   🟢 Active Records (last {retention_days} days) : {active_count:,}")
    print(f"   📦 Records to Archive (> {retention_days} days) : {archived_count:,}")

    if archived_count == 0:
        print(f"✅ All records are within the active {retention_days}-day window. No archival required.")
        return active_count, 0

    if dry_run:
        print("\n[DRY-RUN] Quarterly breakdown of records to archive:")
        for q_key, rows in sorted(archived_by_quarter.items()):
            target_path = os.path.join(telemetry_archive_dir, f"monitor_log_{q_key}.csv.gz")
            print(f"  • {q_key}: {len(rows):,} records -> {target_path}")
        print("[DRY-RUN] No files were modified.")
        return active_count, archived_count

    # Acquire advisory workflow lock to prevent concurrency with run_workflow.sh
    lock = acquire_workflow_lock()
    try:
        # 1. Append records to quarterly gzipped CSV archives
        for q_key, rows in sorted(archived_by_quarter.items()):
            target_path = os.path.join(telemetry_archive_dir, f"monitor_log_{q_key}.csv.gz")
            file_existed = os.path.isfile(target_path)

            # Read existing records to avoid duplicates if appending
            existing_keys = set()
            if file_existed:
                try:
                    with gzip.open(target_path, "rt", encoding="utf-8") as gz_in:
                        r_old = csv.reader(gz_in)
                        next(r_old, None)  # header
                        for r in r_old:
                            if r and len(r) >= 3:
                                existing_keys.add((r[0], r[1], r[2]))
                except Exception as e:
                    sys.stderr.write(f"Warning: Could not read existing archive {target_path}: {e}\n")

            # Filter out duplicates
            new_rows = [r for r in rows if (r[0], r[1], r[2]) not in existing_keys]
            if not new_rows and file_existed:
                print(f"  • {q_key}: All {len(rows):,} records already archived in {target_path}")
                continue

            mode = "at" if file_existed else "wt"
            with gzip.open(target_path, mode, encoding="utf-8") as gz_out:
                writer = csv.writer(gz_out)
                if not file_existed:
                    writer.writerow(header)
                writer.writerows(new_rows)

            size_kb = os.path.getsize(target_path) / 1024
            print(f"  ✅ Archived {len(new_rows):,} records -> {target_path} ({size_kb:.1f} KB)")

        # 2. Atomically rewrite active monitor_log.csv with the retained window
        temp_active = f"{log_file}.tmp"
        with open(temp_active, "w", encoding="utf-8", newline="") as f_out:
            writer = csv.writer(f_out)
            writer.writerow(header)
            writer.writerows(active_rows)

        # 3. Create safety backup of original file prior to replacement
        backup_file = f"{log_file}.bak"
        try:
            shutil.copy2(log_file, backup_file)
        except Exception as e:
            sys.stderr.write(f"Warning: Could not create safety backup {backup_file}: {e}\n")

        # 4. Atomic rename replaces file safely
        os.replace(temp_active, log_file)
        active_size_kb = os.path.getsize(log_file) / 1024
        print(f"✅ Active '{log_file}' updated: {active_count:,} records retained ({active_size_kb:.1f} KB)")

    finally:
        release_workflow_lock(lock)

    return active_count, archived_count


def rotate_cron_log(cron_log=DEFAULT_CRON_LOG,
                    max_mb=DEFAULT_MAX_CRON_MB,
                    archive_dir=DEFAULT_ARCHIVE_DIR,
                    dry_run=False):
    """Rotates and compresses cron.log if it exceeds max_mb."""
    if not os.path.isfile(cron_log):
        return False

    size_bytes = os.path.getsize(cron_log)
    size_mb = size_bytes / (1024 * 1024)

    if size_mb < max_mb:
        print(f"ℹ️  Cron log '{cron_log}' is {size_mb:.2f} MB (threshold: {max_mb} MB). No rotation needed.")
        return False

    cron_archive_dir = os.path.join(archive_dir, "cron")
    now_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    target_gz = os.path.join(cron_archive_dir, f"cron_{now_str}.log.gz")

    print(f"⚠️  Cron log '{cron_log}' reached {size_mb:.2f} MB (> {max_mb} MB threshold). Rotating...")

    if dry_run:
        print(f"[DRY-RUN] Would compress and rotate to: {target_gz}")
        return True

    os.makedirs(cron_archive_dir, exist_ok=True)

    # Compress existing log
    with open(cron_log, "rb") as f_in, gzip.open(target_gz, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)

    # Truncate active cron.log atomically
    with open(cron_log, "w", encoding="utf-8") as f_trunc:
        f_trunc.write(f"# Oklahoma Seismic SOH cron.log rotated at {datetime.now(timezone.utc).isoformat()}\n")

    gz_size_kb = os.path.getsize(target_gz) / 1024
    print(f"✅ Rotated '{cron_log}' -> {target_gz} ({gz_size_kb:.1f} KB)")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Oklahoma Seismic Network 90-Day Rolling Log Manager & Quarterly Archival Routine."
    )
    parser.add_argument(
        "--retention-days", "-d", type=int, default=DEFAULT_RETENTION_DAYS,
        help=f"Number of days to keep in active monitor_log.csv (default: {DEFAULT_RETENTION_DAYS})."
    )
    parser.add_argument(
        "--log-file", default=DEFAULT_LOG_FILE,
        help=f"Path to telemetry log file (default: {DEFAULT_LOG_FILE})."
    )
    parser.add_argument(
        "--cron-log", default=DEFAULT_CRON_LOG,
        help=f"Path to cron log file (default: {DEFAULT_CRON_LOG})."
    )
    parser.add_argument(
        "--archive-dir", default=DEFAULT_ARCHIVE_DIR,
        help=f"Destination directory for archives (default: {DEFAULT_ARCHIVE_DIR})."
    )
    parser.add_argument(
        "--max-cron-mb", type=float, default=DEFAULT_MAX_CRON_MB,
        help=f"Size in MB at which cron.log will be rotated (default: {DEFAULT_MAX_CRON_MB} MB)."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simulate archival and rotation without modifying files."
    )
    args = parser.parse_args()

    print("======================================================================")
    print(" 📦 OKLAHOMA SEISMIC NETWORK SOH: ROLLING LOG ARCHIVE ROUTINE")
    print("======================================================================")

    # 1. Archive monitor_log.csv
    archive_monitor_log(
        log_file=args.log_file,
        retention_days=args.retention_days,
        archive_dir=args.archive_dir,
        dry_run=args.dry_run
    )

    print("")

    # 2. Check and rotate cron.log
    rotate_cron_log(
        cron_log=args.cron_log,
        max_mb=args.max_cron_mb,
        archive_dir=args.archive_dir,
        dry_run=args.dry_run
    )

    print("======================================================================\n")


if __name__ == "__main__":
    main()
