#!/usr/bin/env python3
"""
import_airports.py - Build data/airports.toml from a DCS terrain dump.

Run this after taking a dump with scripts/dcs/dump_airbases_hook.lua, and again
whenever a DCS patch changes the terrain. It is not part of the nightly job: it
rewrites everything the sim knows about an airfield.

Two fields are deliberately left to the nightly feed and are carried over from
the existing file rather than set here:

  * navaids.adf and navaids.fm come from the mission's logistics beacons
  * display comes from whether the airfield is inside the playable boundary

Usage: import_airports.py [OPTIONS] <dump.json>
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import tomllib
from pathlib import Path
from typing import Any


def _missing_dependency(error: ModuleNotFoundError) -> SystemExit:
    return SystemExit(
        f"Missing dependency: {error.name}\n"
        f"  running: {sys.executable}\n"
        "  install: pip install -r scripts/requirements.txt (or `just install`)"
    )


try:
    import mgrs
    from pygeomag import GeoMag
except ModuleNotFoundError as _error:
    raise _missing_dependency(_error) from None

sys.path.insert(0, str(Path(__file__).resolve().parent))

from locations_toml import AIRPORT_TABLE, AIRPORTS_PATH, emit

FEET_PER_METRE = 3.280839895

# Beacon `type` values, decoded by matching against known-good hand-entered data.
# TACAN reports a channel; the rest report a frequency in Hz.
BEACON_TACAN = 4
BEACON_KINDS = {
    3: ("vor", "frequency", 1e6),
    5: ("vor", "frequency", 1e6),  # VORTAC: the VOR half, with a TACAN channel too
    8: ("ndb", "frequency", 1e3),
}

# Airfield radio blocks key their frequencies by index rather than by name.
RADIO_BANDS = {"0": "hf", "1": "fm", "2": "vhf", "3": "uhf"}

# Fields the nightly feed owns. Carried over from the existing file so an import
# does not wipe them; see update_airports.py.
FEED_OWNED_NAVAIDS = ("adf", "fm")

# Decimal year for the magnetic model. Runway designators are only precise to
# 10 degrees and declination across this map spans about one degree, so the
# exact epoch matters little, but it should track the mission's era.
DEFAULT_EPOCH = 2026.5

_MGRS = mgrs.MGRS()
_GEOMAG = GeoMag()

log = logging.getLogger("import_airports")


def dms(value: float, positive: str, negative: str) -> dict[str, Any]:
    direction = positive if value >= 0 else negative
    total = round(abs(value) * 3600)
    degrees, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    return {
        "direction": direction,
        "degrees": int(degrees),
        "minutes": int(minutes),
        "seconds": int(seconds),
    }


def mgrs_parts(latitude: float, longitude: float) -> dict[str, str]:
    raw = _MGRS.toMGRS(latitude, longitude, MGRSPrecision=5)
    return {
        "zone_number": raw[:2],
        "zone_band": raw[2],
        "grid": raw[3:5],
        "easting": raw[5:10],
        "northing": raw[10:15],
    }


def position(airbase: dict[str, Any]) -> tuple[float, float]:
    """The airfield reference point, preferring the terrain's own designation.

    A few FOBs ship reference_point_geo as 0,0; for those the mission's
    getPoint is the only position available.
    """
    geo = (airbase.get("terrain") or {}).get("reference_point_geo") or {}
    latitude, longitude = geo.get("lat"), geo.get("lon")
    if latitude or longitude:
        return latitude, longitude
    log.debug("%s has no terrain reference point; using getPoint", airbase["name"])
    return airbase["latitude"], airbase["longitude"]


def magnetic(true_degrees: float, latitude: float, longitude: float, epoch: float) -> int:
    """True to magnetic, rounded to a whole degree.

    DCS models its own variation, which may differ from the WMM by a fraction of
    a degree. That is well inside the precision a runway heading is read to.
    """
    declination = _GEOMAG.calculate(glat=latitude, glon=longitude, alt=0, time=epoch).d
    return round((true_degrees - declination) % 360)


def designator(value: Any) -> str:
    """Zero-pad a runway number, keeping any L/C/R suffix. DCS reports bare ints."""
    text = str(value or "??").strip().upper()
    digits = "".join(c for c in text if c.isdigit())
    suffix = "".join(c for c in text if c in "LCR")
    return f"{int(digits):02d}{suffix}" if digits else text


def runways(airbase: dict[str, Any], latitude: float, longitude: float, epoch: float) -> list:
    """Pair the terrain's geometry with the mission's dimensions.

    The terrain knows which designator sits at which end and the true course;
    the mission knows length, width and the surface elevation. Both list runways
    in the same order.
    """
    from_terrain = (airbase.get("terrain") or {}).get("runway_list") or []
    from_mission = airbase.get("runways") or []

    out = []
    for index, geometry in enumerate(from_terrain):
        dimensions = from_mission[index] if index < len(from_mission) else {}

        # `course` is radians, true, measured from edge1 towards edge2, so it is
        # the heading of the runway named at edge1.
        primary_true = math.degrees(geometry.get("course", 0.0)) % 360
        primary_heading = magnetic(primary_true, latitude, longitude, epoch)

        elevation = (dimensions.get("position") or {}).get("y")
        length = dimensions.get("length")
        runway: dict[str, Any] = {}
        if elevation is not None:
            runway["elevation_feet"] = str(round(elevation * FEET_PER_METRE))
        if length is not None:
            runway["length_feet"] = str(round(length * FEET_PER_METRE))
        ends = [
            (designator(geometry.get("edge1name")), primary_heading),
            (designator(geometry.get("edge2name")), (primary_heading + 180) % 360),
        ]
        # Terrain lists whichever end it likes first; convention names a runway
        # by its lower designator, so 07/25 rather than 25/07.
        ends.sort(key=lambda end: int(end[0][:2]) if end[0][:2].isdigit() else 99)

        runway["primary"] = {"name": ends[0][0], "heading": f"{ends[0][1]:03d}"}
        runway["secondary"] = {"name": ends[1][0], "heading": f"{ends[1][1]:03d}"}
        out.append(runway)
    return out


def navaids(airbase: dict[str, Any], beacons: dict[str, dict[str, Any]]) -> dict[str, str]:
    """TACAN, VOR and NDB from the terrain's beacon list.

    getBeacons returns every beacon on the map, not this airfield's, so match on
    the ids the airfield's own config block names.
    """
    out: dict[str, str] = {}
    ids = (airbase.get("terrain") or {}).get("beacon_ids") or {}
    if isinstance(ids, dict):
        ids = list(ids.values())

    for entry in ids:
        beacon_id = entry.get("beaconId") if isinstance(entry, dict) else entry
        beacon = beacons.get(beacon_id)
        if not beacon:
            continue

        if beacon.get("type") == BEACON_TACAN or (
            beacon.get("type") == 5 and beacon.get("channel")
        ):
            channel = beacon.get("channel")
            if channel:
                # DCS beacons do not record the X/Y mode; X is the norm.
                out["tacan"] = f"{int(channel)}X"

        kind = BEACON_KINDS.get(beacon.get("type"))
        if kind:
            field, source, divisor = kind
            value = beacon.get(source)
            if value:
                out[field] = f"{value / divisor:.2f}"
    return out


def frequencies(airbase: dict[str, Any]) -> dict[str, str]:
    """The airfield's own ATC frequencies, in Hz, keyed by band index."""
    terrain = airbase.get("terrain") or {}
    ids = terrain.get("radio_ids") or {}
    if isinstance(ids, dict):
        ids = list(ids.values())
    wanted = {entry.get("radioId") if isinstance(entry, dict) else entry for entry in ids}

    # getRadio also returns the whole map, so pick out this airfield's block.
    for block in terrain.get("radio") or []:
        if block.get("radioId") not in wanted:
            continue
        out = {}
        for index, band in RADIO_BANDS.items():
            value = (block.get("frequency") or {}).get(index)
            if value:
                # Three decimals throughout, so 240.300 does not read as 240.3
                # next to 123.350 on the kneeboard.
                out[band] = f"{value[1] / 1e6:.3f}"
        return out
    return {}


def build(dump: dict[str, Any], existing: dict[str, dict[str, Any]], epoch: float) -> list:
    beacons = {b["beaconId"]: b for b in dump.get("beacons", []) if b.get("beaconId")}
    airports = []

    for airbase in dump.get("airbases", []):
        name = str(airbase["name"]).replace("_", " ").upper()
        latitude, longitude = position(airbase)
        previous = existing.get(name, {})

        entry: dict[str, Any] = {
            "type": "airport",
            "name": name,
            # Owned by the nightly feed; keep whatever it last decided.
            "display": bool(previous.get("display", False)),
            "elevation_feet": str(round(airbase["elevation_m"] * FEET_PER_METRE)),
            "coordinates": {
                "dms": {
                    "latitude": dms(latitude, "N", "S"),
                    "longitude": dms(longitude, "E", "W"),
                },
                "mgrs": mgrs_parts(latitude, longitude),
            },
        }

        icao = ((airbase.get("terrain") or {}).get("icao") or "").strip()
        if icao:
            entry["icao"] = icao

        beacon_navaids = navaids(airbase, beacons)
        carried = {
            key: value
            for key, value in (previous.get("navaids") or {}).items()
            if key in FEED_OWNED_NAVAIDS
        }
        if beacon_navaids or carried:
            entry["navaids"] = {**beacon_navaids, **carried}

        radio = frequencies(airbase)
        if radio:
            entry["frequencies"] = radio

        runway_list = runways(airbase, latitude, longitude, epoch)
        if runway_list:
            entry["runways"] = runway_list

        airports.append(entry)
    return airports


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="import_airports.py",
        description="Build data/airports.toml from a DCS terrain dump.",
    )
    parser.add_argument("dump", type=Path, help="JSON written by dump_airbases_hook.lua")
    parser.add_argument("-o", "--output", type=Path, default=Path(AIRPORTS_PATH))
    parser.add_argument(
        "--epoch",
        type=float,
        default=DEFAULT_EPOCH,
        help=f"decimal year for magnetic declination (default: {DEFAULT_EPOCH})",
    )
    parser.add_argument("--dry-run", action="store_true", help="do not write anything")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_arguments(argv)
    level_threshold = (
        logging.DEBUG if args.verbose else logging.ERROR if args.quiet else logging.INFO
    )
    for level, name in (
        (logging.DEBUG, "DEBUG:"),
        (logging.INFO, "INFO:"),
        (logging.WARNING, "WARNING:"),
        (logging.ERROR, "ERROR:"),
        (logging.CRITICAL, "CRITICAL:"),
    ):
        logging.addLevelName(level, name)
    logging.basicConfig(
        level=level_threshold,
        format="[%(asctime)s] %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )

    if not args.dump.is_file():
        log.error("Dump not found: %s", args.dump)
        return 2

    dump = json.loads(args.dump.read_text(encoding="utf-8"))
    if not dump.get("airbases"):
        log.error("%s has no airbases", args.dump)
        return 1
    log.info("Theatre %s, %d airbases", dump.get("theatre", "?"), len(dump["airbases"]))

    existing: dict[str, dict[str, Any]] = {}
    if args.output.is_file():
        existing = {
            entry["name"]: entry
            for entry in tomllib.loads(args.output.read_text(encoding="utf-8")).get(
                AIRPORT_TABLE, []
            )
        }

    airports = build(dump, existing, args.epoch)
    text = emit(airports, AIRPORT_TABLE)

    carried = sum(1 for a in airports if a["name"] in existing)
    log.info(
        "%d airports (%d already in the file, keeping their display flag and ADF/FM)",
        len(airports),
        carried,
    )

    if args.dry_run:
        return 0
    args.output.write_text(text, encoding="utf-8")
    log.info("Wrote %s", args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
