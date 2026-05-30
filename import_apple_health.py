"""
import_apple_health.py — One-time bulk import of weight history from
an Apple Health export.

Why this exists (issue #20): Apple Health stores weight data merged from
multiple sources (Withings, manual entry, third-party apps, …). The
Withings API itself only returns readings measured by Withings devices,
so historical readings from before your current Withings setup are
unreachable through `withings_sync.py`. Apple Health, however, retains
everything — exporting from there and importing here is the simplest
way to backfill pre-Withings history.

Usage:
    # 1. On your iPhone: open the Health app → tap your profile picture
    #    (top right) → scroll to "Export All Health Data".
    # 2. iOS produces a ZIP (typically named "export.zip"). Transfer it
    #    to the server (AirDrop to Mac → scp, for example).
    # 3. Run this script:
    python import_apple_health.py --user 264886463 --file export.zip

The DB's UNIQUE(user_id, measured_at) constraint makes the script
idempotent — re-running won't double-insert. Readings get the source
tag "apple_health" so they're distinguishable from withings_api ones.
"""

import argparse
import logging
import sys
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, BinaryIO

import database as db

# Apple Health record type for weight measurements.
WEIGHT_TYPE = "HKQuantityTypeIdentifierBodyMass"

# Conversion factors to kilograms. Apple may export in different units
# depending on the phone's region/settings.
_TO_KG = {
    "kg": 1.0,
    "g": 0.001,
    "lb": 0.45359237,
    "lbs": 0.45359237,
}

log = logging.getLogger("apple_health_import")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def _parse_apple_date(s: str) -> Optional[str]:
    """'2022-03-15 07:42:00 +0100' → '2022-03-15T06:42:00Z' (UTC ISO).

    Returns None on any parse failure — caller skips the record.
    """
    if not s:
        return None
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S %z")
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return None


def _to_kg(value_str: Optional[str], unit: Optional[str]) -> Optional[float]:
    """Convert to kilograms. Returns None for unknown units or bad numbers."""
    if value_str is None:
        return None
    try:
        value = float(value_str)
    except (TypeError, ValueError):
        return None
    factor = _TO_KG.get((unit or "").lower())
    if factor is None:
        return None
    return value * factor


def _open_export_xml(file_path: Path) -> BinaryIO:
    """Return a binary file-like for the export.xml inside `file_path`.
    Handles both the raw `.xml` file and the iOS ZIP-export format
    (the latter contains `apple_health_export/export.xml` plus other
    sibling files like `export_cda.xml` — we match the basename
    exactly so we don't grab the wrong one).
    """
    if zipfile.is_zipfile(file_path):
        zf = zipfile.ZipFile(file_path)
        candidates = [n for n in zf.namelist() if Path(n).name == "export.xml"]
        if not candidates:
            zf.close()
            raise RuntimeError(
                f"No export.xml found inside {file_path}. "
                f"First 5 entries: {zf.namelist()[:5]}"
            )
        # zf stays implicitly open as long as the inner stream is open; the
        # caller calls .close() on the returned stream, which closes the
        # zipfile descriptor too.
        return zf.open(candidates[0])
    return open(file_path, "rb")


def import_file(
    user_id: int,
    file_path: Path,
    dry_run: bool = False,
) -> dict:
    """Stream-parse the XML and write each weight record. Returns stats."""
    if not file_path.exists():
        raise FileNotFoundError(file_path)

    total = 0
    inserted = 0
    skipped_dup = 0
    skipped_bad = 0
    earliest: Optional[str] = None
    latest: Optional[str] = None

    stream = _open_export_xml(file_path)
    try:
        # iterparse + elem.clear() keeps memory bounded for multi-GB
        # exports. Several years of Apple Health data can easily exceed
        # a gigabyte of XML.
        for event, elem in ET.iterparse(stream, events=("end",)):
            if elem.tag != "Record" or elem.attrib.get("type") != WEIGHT_TYPE:
                elem.clear()
                continue

            total += 1
            value_kg = _to_kg(elem.attrib.get("value"), elem.attrib.get("unit"))
            measured_at = _parse_apple_date(elem.attrib.get("startDate"))
            elem.clear()   # release the record's memory ASAP

            if value_kg is None or measured_at is None:
                skipped_bad += 1
                continue

            if earliest is None or measured_at < earliest:
                earliest = measured_at
            if latest is None or measured_at > latest:
                latest = measured_at

            if dry_run:
                continue

            if db.insert_weight_reading(
                user_id=user_id,
                measured_at=measured_at,
                weight_kg=value_kg,
                source="apple_health",
            ):
                inserted += 1
            else:
                skipped_dup += 1

            if total % 1000 == 0:
                log.info(
                    f"...processed {total} weight records "
                    f"(new={inserted}, dups={skipped_dup}, bad={skipped_bad})"
                )
    finally:
        stream.close()

    return {
        "total": total,
        "inserted": inserted,
        "skipped_duplicates": skipped_dup,
        "skipped_bad": skipped_bad,
        "earliest": earliest,
        "latest": latest,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-time import of weight history from Apple Health export",
    )
    parser.add_argument("--user", type=int, required=True,
                        help="Telegram user_id to import readings to.")
    parser.add_argument("--file", type=str, required=True,
                        help="Path to Apple Health export (ZIP or XML).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse + count, do NOT write to the DB.")
    args = parser.parse_args()

    db.init_db()
    db.ensure_user(args.user)

    file_path = Path(args.file).expanduser()
    log.info(f"User {args.user}: importing from {file_path}")
    if args.dry_run:
        log.info("DRY RUN — no rows will be written")

    try:
        stats = import_file(args.user, file_path, dry_run=args.dry_run)
    except FileNotFoundError as e:
        log.error(f"File not found: {e}")
        sys.exit(1)
    except RuntimeError as e:
        log.error(str(e))
        sys.exit(1)

    log.info("───── Summary ─────")
    log.info(f"  Weight records found:   {stats['total']}")
    log.info(f"  Newly inserted:         {stats['inserted']}")
    log.info(f"  Skipped (duplicate):    {stats['skipped_duplicates']}")
    log.info(f"  Skipped (bad value):    {stats['skipped_bad']}")
    if stats["earliest"]:
        log.info(f"  Date range:             {stats['earliest']} → {stats['latest']}")


if __name__ == "__main__":
    main()
