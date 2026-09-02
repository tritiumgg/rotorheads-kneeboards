#!/usr/bin/env python3
"""
update_airports.py - Refresh the two airport fields the live map feed owns.

Everything else in data/airports.toml comes from a terrain dump and is left
alone; see import_airports.py.

  * navaids.adf and navaids.fm come from the mission's `Logistics -` markers.
    An airfield with no marker has its homing beacons cleared: a stale frequency
    is worse than a blank one.
  * display comes from whether the airfield sits inside the mission's playable
    boundary polygon, which moves as the campaign progresses.

Usage: update_airports.py [OPTIONS]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
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
except ModuleNotFoundError as _error:
    raise _missing_dependency(_error) from None

sys.path.insert(0, str(Path(__file__).resolve().parent))

from locations_toml import AIRPORT_TABLE, AIRPORTS_PATH, emit

DEFAULT_MAP_DATA_PATH = "build/map.json"

# An airfield's logistics marker. Unlike a FARP marker there is no crates line,
# which is what keeps the two apart.
LOGISTICS_RE = re.compile(
    r"""^
    Logistics\ -\ (?P<name>[^\n]+)\n
    ADF:\s*(?P<adf>[0-9.]+)\s*kHz\s*/\s*FM:\s*(?P<fm>[0-9.]+)\s*MHz\s*\n
    \s*
    Status:\s*(?P<status>[^\n]+?)\s*
    $""",
    re.VERBOSE,
)

# Fields this script owns. Everything else in an entry is left untouched.
FEED_OWNED_NAVAIDS = ("adf", "fm")

_MGRS = mgrs.MGRS()

log = logging.getLogger("update_airports")


def position(entry: dict[str, Any]) -> tuple[float, float] | None:
    """Read the metre-precision MGRS rather than the whole-second DMS."""
    grid = (entry.get("coordinates") or {}).get("mgrs")
    if not grid:
        return None
    try:
        return _MGRS.toLatLon(
            "{zone_number}{zone_band}{grid}{easting}{northing}".format(**grid)
        )
    except Exception:
        log.warning("%s has unreadable MGRS", entry.get("name"))
        return None


def boundary(feed: dict[str, Any]) -> list[list[float]] | None:
    """The mission's playable area.

    It is the only polygon in the feed and carries no icon, so match on geometry
    type. If that ever stops being true, take the largest and say so rather than
    silently picking one.
    """
    rings = []
    for feature in feed.get("features", []):
        geometry = feature.get("geometry") or {}
        if geometry.get("type") != "Polygon":
            continue
        coordinates = geometry.get("coordinates") or []
        if coordinates and len(coordinates[0]) >= 4:
            rings.append(coordinates[0])

    if not rings:
        return None
    if len(rings) > 1:
        log.warning("%d polygons in the feed; using the largest", len(rings))
        rings.sort(key=ring_area, reverse=True)
    return rings[0]


def ring_area(ring: list[list[float]]) -> float:
    """Shoelace, in square degrees. Only used to rank candidate polygons."""
    total = 0.0
    for index in range(len(ring) - 1):
        x1, y1 = ring[index]
        x2, y2 = ring[index + 1]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2


def inside(longitude: float, latitude: float, ring: list[list[float]]) -> bool:
    """Ray casting. The boundary spans a few degrees, so plain lon/lat is fine."""
    hit = False
    for index in range(len(ring) - 1):
        x1, y1 = ring[index]
        x2, y2 = ring[index + 1]
        if (y1 > latitude) != (y2 > latitude):
            crossing = x1 + (latitude - y1) * (x2 - x1) / (y2 - y1)
            if longitude < crossing:
                hit = not hit
    return hit


def logistics(feed: dict[str, Any]) -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    for feature in feed.get("features", []):
        properties = feature.get("properties") or {}
        if properties.get("icon") != "mark":
            continue
        match = LOGISTICS_RE.match((properties.get("description") or "").strip())
        if match:
            found[match.group("name").strip().upper()] = match.groupdict()
    return found


def update(
    airports: list[dict[str, Any]],
    markers: dict[str, dict[str, str]],
    ring: list[list[float]] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    report: list[str] = []
    updated: list[dict[str, Any]] = []
    matched: set[str] = set()

    for entry in airports:
        entry = {**entry}
        name = entry["name"].upper()
        marker = markers.get(name)
        if marker:
            matched.add(name)

        navaids = {**(entry.get("navaids") or {})}
        was = {key: navaids.get(key) for key in FEED_OWNED_NAVAIDS}
        if marker:
            navaids["adf"] = marker["adf"]
            navaids["fm"] = marker["fm"]
        else:
            for key in FEED_OWNED_NAVAIDS:
                navaids.pop(key, None)
        now = {key: navaids.get(key) for key in FEED_OWNED_NAVAIDS}

        for key in FEED_OWNED_NAVAIDS:
            if was[key] != now[key]:
                report.append(
                    f"- **{entry['name']}** {key.upper()}: "
                    f"{was[key] or 'none'} to {now[key] or 'none'}"
                )

        if navaids:
            entry["navaids"] = navaids
        else:
            entry.pop("navaids", None)

        if ring is not None:
            coordinates = position(entry)
            if coordinates is None:
                log.warning("%s has no readable position; leaving display alone", name)
            else:
                latitude, longitude = coordinates
                shown = inside(longitude, latitude, ring)
                if bool(entry.get("display")) != shown:
                    report.append(
                        f"- **{entry['name']}** is now "
                        f"{'inside' if shown else 'outside'} the playable boundary, "
                        f"so display becomes {str(shown).lower()}"
                    )
                entry["display"] = shown

        updated.append(entry)

    for name in sorted(set(markers) - matched):
        # Worth surfacing: the feed knows an airfield the file does not, which
        # usually means the terrain import needs re-running.
        report.append(
            f"- **{name}** has a logistics beacon but is not in the file; "
            "re-run `import_airports.py` if it should be"
        )

    return updated, report


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="update_airports.py",
        description="Refresh airport ADF/FM and display from the live map feed.",
    )
    parser.add_argument("--map-data", type=Path, default=Path(DEFAULT_MAP_DATA_PATH))
    parser.add_argument("--airports", type=Path, default=Path(AIRPORTS_PATH))
    parser.add_argument("-o", "--output", type=Path, help="write here instead of in place")
    parser.add_argument(
        "--carry-from",
        type=Path,
        help="read the current entries from this file instead of --airports",
    )
    parser.add_argument("--report", type=Path, help="write the Markdown change report here")
    parser.add_argument("--dry-run", action="store_true", help="do not write any files")
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

    if not args.airports.is_file():
        log.error("Airport file not found: %s", args.airports)
        return 2
    if not args.map_data.is_file():
        log.error("Map data not found: %s. Run scripts/fetch-map-data.sh first.", args.map_data)
        return 2

    original_text = args.airports.read_text(encoding="utf-8")
    source = args.carry_from if args.carry_from and args.carry_from.is_file() else args.airports
    airports = tomllib.loads(source.read_text(encoding="utf-8")).get(AIRPORT_TABLE, [])
    if not airports:
        log.error("No airports in %s", source)
        return 1

    try:
        feed = json.loads(args.map_data.read_text(encoding="utf-8"))
    except Exception as error:
        log.error("Could not read %s: %s", args.map_data, error)
        return 1

    markers = logistics(feed)
    ring = boundary(feed)
    if ring is None:
        # Without it every airfield would flip to hidden, which would empty the
        # kneeboard. Leave the flags as they are.
        log.warning("No playable boundary in the feed; leaving display flags alone")
    log.info("%d logistics marker(s), boundary %s", len(markers), "found" if ring else "missing")

    updated, report = update(airports, markers, ring)
    updated_text = emit(updated, AIRPORT_TABLE)
    changed = updated_text != original_text

    summary = "\n".join(report) if report else "No airport changes."
    if changed and not report:
        summary = "Reformatted `airports.toml`; no airport data changed."
    log.info("changed=%s shown=%d/%d", changed, sum(1 for a in updated if a.get("display")), len(updated))
    if not args.quiet:
        print(summary)

    if not args.dry_run:
        (args.output or args.airports).write_text(updated_text, encoding="utf-8")
        if args.report:
            args.report.write_text(summary + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
