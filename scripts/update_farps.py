#!/usr/bin/env python3
"""
update_farps.py - Rebuild data/farps.toml from the RotorHeads live map JSON feed.

Every entry is regenerated from the feed, except for fields the feed does not
expose (elevation, and a hand-corrected shortname), which are carried over from
the existing file. Airports live in data/airports.toml and are not touched here.

Usage: update_farps.py [OPTIONS]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from locations_toml import FARP_TABLE, FARPS_PATH, UNKNOWN_ELEVATION, emit


def _missing_dependency(error: ModuleNotFoundError) -> SystemExit:
    """A traceback here means "not installed", which is worth saying plainly.

    Printing the interpreter path matters when mise is in play: the usual cause
    is that the packages went into a different Python than the one running this.
    """
    return SystemExit(
        f"Missing dependency: {error.name}\n"
        f"  running: {sys.executable}\n"
        "  install: pip install -r scripts/requirements.txt (or `just install`)"
    )


try:
    import mgrs
    from pyproj import Geod, Transformer
except ModuleNotFoundError as _error:
    raise _missing_dependency(_error) from None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Downloaded by scripts/fetch-map-data.sh. Nothing here knows the feed's address:
# this script only ever reads a file, so a run is reproducible and can be pointed
# at a saved copy of the map data instead.
DEFAULT_MAP_DATA_PATH = "build/map.json"

# The live map places each FARP marker ~100 m east (grid east, WGS84 / UTM) of
# the coordinate the server publishes as the FARP itself. Verified against the
# ALPHA / BRAVO / CHARLIE "Spawn Point" features, which sit at exactly this
# offset from their matching FARP marker, and against every hand-entered
# coordinate that was in the file at the time (dE = -100.0 m, dN = 0 m, n=8).
# Set to 0.0 to use raw marker coordinates.
DEFAULT_EASTING_OFFSET_M = -100.0

# FARP names are drawn from a fixed pool by the mission, so a decommissioned
# FARP's name can be reissued to an unrelated pad elsewhere on the map. A match
# on name alone is therefore not proof of identity: if the position has moved
# further than this, the FARP is treated as brand new and nothing manual carries
# over from the old entry. Run-to-run drift for a FARP that has not moved is
# under 10 m in the observed data, so this has a wide safety margin.
DEFAULT_REBUILD_THRESHOLD_M = 50.0

# Sanity guard: a mission restart or a failed export can produce a feed with no
# FARPs at all. Refuse to wipe the file in that case.
DEFAULT_MIN_FARPS = 1

# Fields parsed from the feed but not currently written to the TOML.
# Add "crates" and/or "status" here to start tracking them; see FarpRecord.to_toml()
# for where they get emitted, and CHANGE_TRACKED_FIELDS for change detection.
EXTRA_EMITTED_FIELDS: tuple[str, ...] = ()

# Nav systems take short, uppercase, alphanumeric identifiers. Six characters is
# the working assumption; raise it here if the DCS route planner turns out to
# accept more.
SHORTNAME_MAX_LENGTH = 6

# Y is a consonant during normal reduction and only becomes droppable as a last
# resort, so VINTOKRYL loses its O before its Y but SKYRAIDER keeps SKYRDR.
SHORTNAME_VOWELS = "AEIOU"
SHORTNAME_LAST_RESORT_VOWELS = "AEIOUY"

# Applied one rule at a time to the ORIGINAL spelling, re-reducing after each,
# so only as many fire as are needed. Order is preference order: safest first.
# QU needs no rule because U is a vowel and is dropped for free, and neither do
# vowel digraphs like EA/OU/IE.
SHORTNAME_DIGRAPHS: tuple[tuple[str, str], ...] = (
    (r"TCH", "CH"),   # watchtower, hatchet - the T is pure redundancy
    (r"DG", "G"),     # stonebridge, ridgeline, sledgehammer
    (r"WH", "W"),     # whirlwind, whiskey, whitehorse
    (r"CK", "K"),     # blackthorne, checkpoint, silverback
    (r"GH", "G"),     # ghost, and silent-GH in lightning and nighthawk
    (r"PH", "F"),     # phantom, sapphire
    (r"RH", "R"),     # thunderhead, rhodes
    (r"^KN", "N"),    # knight - initial only, or acknowledge breaks
    (r"^WR", "R"),    # wrangler - initial only
    (r"^GN", "N"),    # gnome
    (r"MB$", "M"),    # climb - final only, or combat breaks
    (r"SCH", "SH"),   # schooner
    (r"TH", "T"),     # thunder, blackthorne
    (r"SH", "S"),     # shields, kingfisher
    (r"CH", "C"),     # chesterfield, chariot
)

# Hand-written shortnames for names the algorithm mangles. Only consulted for
# FARPs new to the file; an edit made directly in farps.toml also sticks,
# because existing shortnames are carried across untouched.
SHORTNAME_OVERRIDES: dict[str, str] = {
    # "CHESTERFIELD": "CHSTFD",
}

# Fields the generator carries forward from the existing file rather than
# deriving from the feed. Everything else on a FARP is rewritten every run, so
# only these can ever go stale.
CARRIED_FIELDS = ("shortname", "elevation_feet")

# Of those, the ones tied to where the pad physically is. When a name is reused
# by a FARP somewhere else, these are cleared and the rest are kept: a shortname
# is derived from the name alone, so a move cannot invalidate it, and clearing it
# would throw away any hand-edit along with it.
POSITION_DEPENDENT_FIELDS = ("elevation_feet",)

log = logging.getLogger("update_farps")


# ---------------------------------------------------------------------------
# Feed parsing
# ---------------------------------------------------------------------------

# A real FARP marker looks like:
#
#   ALPHA
#   FARP VINTOKRYL
#   ADF: 1150.00 kHz  / FM: 62.00 MHz
#   Crates: 3
#
#   Status: Active
#
# The leading callsign line is optional. Requiring the ADF/FM, Crates and Status
# lines is what keeps player-drawn marks like "Enemy FARP", "Build FARP" and
# "FARP Perimeter" out of the results.
FARP_DESCRIPTION_RE = re.compile(
    r"""^
    (?:(?P<callsign>[A-Z][A-Z0-9 '\-]*)\n)?
    FARP\ (?P<name>[^\n]+)\n
    ADF:\s*(?P<adf>[0-9.]+)\s*kHz\s*/\s*FM:\s*(?P<fm>[0-9.]+)\s*MHz\s*\n
    Crates:\s*(?P<crates>\d+)\s*\n
    \s*
    Status:\s*(?P<status>[^\n]+?)\s*
    $""",
    re.VERBOSE,
)


@dataclass
class FarpRecord:
    """A FARP as published by the live map feed, in TOML-ready form."""

    name: str
    shortname: str
    callsign: str | None
    latitude: float
    longitude: float
    adf: str
    fm: str
    crates: int
    status: str
    elevation_feet: str = UNKNOWN_ELEVATION
    dms: dict[str, dict[str, Any]] = field(default_factory=dict)
    mgrs: dict[str, str] = field(default_factory=dict)
    # Feed position with the easting correction applied; used for identity
    # checks against the previous file contents.
    resolved_latitude: float | None = None
    resolved_longitude: float | None = None

    def to_toml(self) -> dict[str, Any]:
        location: dict[str, Any] = {
            "type": "farp",
            "name": self.name,
            "shortname": self.shortname,
            "elevation_feet": self.elevation_feet,
            "coordinates": {"dms": self.dms, "mgrs": self.mgrs},
            "navaids": {"adf": self.adf, "fm": self.fm},
        }
        if self.callsign:
            location["callsign"] = self.callsign

        # Extension point: crate count and repair status are parsed on every run
        # but only reach the file if opted into via EXTRA_EMITTED_FIELDS.
        if "crates" in EXTRA_EMITTED_FIELDS:
            location["crates"] = str(self.crates)
        if "status" in EXTRA_EMITTED_FIELDS:
            location["status"] = self.status

        return location


def smart_title(text: str) -> str:
    """Title-case a SHOUTED feed name.

    Matches runs of letters only, so punctuation splits words: O'BRIEN becomes
    O'Brien rather than O'brien, which is what including the apostrophe in the
    pattern would give.
    """
    return re.sub(r"[A-Za-z]+", lambda m: m.group(0).capitalize(), text.strip())


def parse_farps(feed: dict[str, Any]) -> list[FarpRecord]:
    farps: list[FarpRecord] = []
    for feature in feed.get("features", []):
        properties = feature.get("properties") or {}
        if properties.get("icon") != "mark":
            continue
        match = FARP_DESCRIPTION_RE.match((properties.get("description") or "").strip())
        if not match:
            continue

        geometry = feature.get("geometry") or {}
        if geometry.get("type") != "Point":
            log.warning("Skipping FARP %s: unexpected geometry", match.group("name"))
            continue
        coordinates = geometry.get("coordinates") or []
        if len(coordinates) < 2:
            log.warning("Skipping FARP %s: incomplete coordinates", match.group("name"))
            continue
        longitude, latitude = coordinates[:2]

        callsign = match.group("callsign")
        farps.append(
            FarpRecord(
                name=match.group("name").strip(),
                shortname="",
                callsign=smart_title(callsign) if callsign else None,
                latitude=float(latitude),
                longitude=float(longitude),
                adf=match.group("adf"),
                fm=match.group("fm"),
                crates=int(match.group("crates")),
                status=match.group("status"),
            )
        )
    return farps


# ---------------------------------------------------------------------------
# Shortnames
# ---------------------------------------------------------------------------


def collapse_doubles(text: str) -> str:
    """SLEDGEHAMMER -> SLEDGEHAMER. Cheaper in legibility than losing a vowel."""
    if not text:
        return text
    kept = [text[0]]
    for character in text[1:]:
        if character != kept[-1]:
            kept.append(character)
    return "".join(kept)


def drop_vowels(text: str, limit: int, vowels: str) -> str:
    """Remove vowels from the end backward, never the first character."""
    characters = list(text)
    for index in range(len(characters) - 1, 0, -1):
        if sum(1 for character in characters if character) <= limit:
            break
        if characters[index] in vowels:
            characters[index] = ""
    return "".join(characters)


def reduce_to_fit(text: str, limit: int, vowels: str = SHORTNAME_VOWELS) -> str:
    collapsed = collapse_doubles(text)
    if len(collapsed) <= limit:
        return collapsed
    return drop_vowels(collapsed, limit, vowels)


def derive_shortname(name: str, limit: int = SHORTNAME_MAX_LENGTH) -> str:
    """Abbreviate a FARP name to a nav-system identifier, deterministically."""
    base = re.sub(r"[^A-Z0-9]", "", name.upper())
    if not base:
        raise ValueError(f"Name {name!r} contains no usable characters")
    if len(base) <= limit:
        return base

    candidate = reduce_to_fit(base, limit)
    if len(candidate) <= limit:
        return candidate

    # Digraphs are matched against the original spelling rather than the
    # consonant skeleton: devowelling DAGGER would otherwise leave DGR, whose
    # DG was never a digraph at all.
    working = base
    for pattern, replacement in SHORTNAME_DIGRAPHS:
        if not re.search(pattern, working):
            continue
        working = re.sub(pattern, replacement, working)
        candidate = reduce_to_fit(working, limit)
        if len(candidate) <= limit:
            return candidate

    # Last resort before cutting characters off: let Y count as a vowel, which
    # keeps a word ending that truncation would throw away.
    candidate = reduce_to_fit(working, limit, SHORTNAME_LAST_RESORT_VOWELS)
    if len(candidate) <= limit:
        return candidate

    return candidate[:limit]


def assign_shortname(name: str, claimed: set[str], limit: int = SHORTNAME_MAX_LENGTH) -> str:
    """Derive a shortname and resolve any clash against already-claimed ones.

    Clashes are resolved by replacing trailing characters with a number, so the
    result never exceeds the length limit.
    """
    candidate = SHORTNAME_OVERRIDES.get(name.upper()) or derive_shortname(name, limit)
    if candidate not in claimed:
        return candidate

    for width, start, stop in ((1, 2, 10), (2, 10, 100)):
        if len(candidate) <= width:
            continue
        for suffix in range(start, stop):
            alternative = candidate[:-width] + str(suffix)
            if alternative not in claimed:
                log.warning(
                    "Shortname %s for %s is taken; using %s", candidate, name, alternative
                )
                return alternative

    raise ValueError(f"Could not find a free shortname for {name!r} (base {candidate})")


# ---------------------------------------------------------------------------
# Coordinates
# ---------------------------------------------------------------------------

_MGRS = mgrs.MGRS()
_GEOD = Geod(ellps="WGS84")
_MGRS_RE = re.compile(r"^(\d{1,2})([A-Z])([A-Z]{2})(\d{5})(\d{5})$")


def utm_zone(longitude: float) -> int:
    return int((longitude + 180.0) // 6.0) + 1


def shift_easting(latitude: float, longitude: float, offset_m: float) -> tuple[float, float]:
    """Move a WGS84 point by offset_m along grid east in its natural UTM zone."""
    if offset_m == 0.0:
        return latitude, longitude
    epsg = 32600 + utm_zone(longitude) if latitude >= 0 else 32700 + utm_zone(longitude)
    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    to_wgs = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    easting, northing = to_utm.transform(longitude, latitude)
    shifted_lon, shifted_lat = to_wgs.transform(easting + offset_m, northing)
    return shifted_lat, shifted_lon


def to_dms(latitude: float, longitude: float) -> dict[str, dict[str, Any]]:
    def component(value: float, positive: str, negative: str) -> dict[str, Any]:
        direction = positive if value >= 0 else negative
        total_seconds = round(abs(value) * 3600.0)
        degrees, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return {
            "direction": direction,
            "degrees": int(degrees),
            "minutes": int(minutes),
            "seconds": int(seconds),
        }

    return {
        "latitude": component(latitude, "N", "S"),
        "longitude": component(longitude, "E", "W"),
    }


def to_mgrs(latitude: float, longitude: float) -> dict[str, str]:
    raw = _MGRS.toMGRS(latitude, longitude, MGRSPrecision=5)
    match = _MGRS_RE.match(raw)
    if not match:
        raise ValueError(f"Unparseable MGRS string: {raw!r}")
    zone_number, zone_band, grid, easting, northing = match.groups()
    return {
        "zone_number": zone_number,
        "zone_band": zone_band,
        "grid": grid,
        "easting": easting,
        "northing": northing,
    }


def resolve_coordinates(farp: FarpRecord, offset_m: float) -> None:
    latitude, longitude = shift_easting(farp.latitude, farp.longitude, offset_m)
    farp.resolved_latitude = latitude
    farp.resolved_longitude = longitude
    farp.dms = to_dms(latitude, longitude)
    farp.mgrs = to_mgrs(latitude, longitude)


def previous_position(location: dict[str, Any]) -> tuple[float, float] | None:
    """Recover lat/lon from a location's stored MGRS, which is metre-precision."""
    grid = (location.get("coordinates") or {}).get("mgrs")
    if not grid:
        return None
    try:
        raw = "{zone_number}{zone_band}{grid}{easting}{northing}".format(**grid)
        return _MGRS.toLatLon(raw)
    except Exception:  # malformed or hand-edited coordinates
        return None


def distance_moved(location: dict[str, Any], farp: FarpRecord) -> float | None:
    """Metres between a location's recorded position and the feed's, or None."""
    previous = previous_position(location)
    if previous is None or farp.resolved_latitude is None:
        return None
    _, _, metres = _GEOD.inv(
        previous[1], previous[0], farp.resolved_longitude, farp.resolved_latitude
    )
    return abs(metres)


# ---------------------------------------------------------------------------
# Merge and diff
# ---------------------------------------------------------------------------

# Fields compared when deciding whether the file needs updating. Add "crates" or
# "status" here (alongside EXTRA_EMITTED_FIELDS) to make them trigger a PR.
CHANGE_TRACKED_FIELDS = (
    "shortname",
    "callsign",
    "coordinates",
    "navaids",
    *EXTRA_EMITTED_FIELDS,
)


def merge_locations(
    existing: list[dict[str, Any]],
    farps: list[FarpRecord],
    rebuild_threshold_m: float = DEFAULT_REBUILD_THRESHOLD_M,
    carry_source: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, float | None]]:
    """Rebuild the FARP entries, carrying manual fields across where identity holds.

    Manual fields come from `carry_source` when given, which lets the caller
    point at an open pull request's copy of the file so an elevation typed into
    that pull request is not thrown away when it gets replaced.

    Returns the merged locations and, for each FARP judged to be a different pad
    reusing an old name, how far it moved (None when the old position could not
    be read).
    """
    if carry_source is None:
        carry_source = existing
    by_upper_name = {entry["name"].upper(): entry for entry in carry_source}

    merged: list[dict[str, Any]] = []
    rebuilt: dict[str, float | None] = {}

    for farp in farps:
        key = farp.name.upper()
        previous = by_upper_name.get(key)

        farp.name = key
        if previous is None:
            continue

        moved = distance_moved(previous, farp)
        is_rebuild = moved is None or moved > rebuild_threshold_m
        if is_rebuild:
            log.info(
                "FARP %s has moved %s; treating it as a new FARP reusing the name",
                farp.name,
                f"{moved:.0f} m" if moved is not None else "an unknown distance",
            )
            rebuilt[key] = moved

        for field_name in CARRIED_FIELDS:
            if is_rebuild and field_name in POSITION_DEPENDENT_FIELDS:
                continue
            setattr(farp, field_name, str(previous.get(field_name, "")))

    # Shortnames already in the file keep their claim, so adding a FARP can never
    # renumber an existing one. New ones are assigned in name order to keep the
    # result independent of the order the feed happened to list them in.
    claimed = {farp.shortname for farp in farps if farp.shortname}
    for farp in sorted(farps, key=lambda item: item.name):
        if farp.shortname:
            continue
        farp.shortname = assign_shortname(farp.name, claimed)
        claimed.add(farp.shortname)

    merged.extend(farp.to_toml() for farp in farps)
    return merged, rebuilt


def flatten(prefix: str, value: Any, out: dict[str, str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            flatten(f"{prefix}.{key}" if prefix else key, child, out)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            flatten(f"{prefix}[{index}]", child, out)
    else:
        out[prefix] = str(value)


def describe_changes(
    existing: list[dict[str, Any]],
    updated: list[dict[str, Any]],
    rebuilt: dict[str, float | None] | None = None,
) -> tuple[list[str], dict[str, int], dict[str, list[str]]]:
    def by_name(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {entry["name"].upper(): entry for entry in entries}

    rebuilt = rebuilt or {}
    before, after = by_name(existing), by_name(updated)
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    # A recycled name is reported as its own category, not as a modification.
    common = sorted((set(before) & set(after)) - set(rebuilt))
    recycled = sorted(set(rebuilt) & set(after))

    report: list[str] = []
    counts = {
        "added": len(added),
        "removed": len(removed),
        "rebuilt": len(recycled),
        "modified": 0,
    }
    changed_names: dict[str, list[str]] = {
        "added": [after[key]["name"] for key in added],
        "removed": [before[key]["name"] for key in removed],
        "rebuilt": [after[key]["name"] for key in recycled],
        "modified": [],
    }
    unsurveyed: list[str] = []

    for key in added:
        location = after[key]
        callsign = f" ({location['callsign']})" if location.get("callsign") else ""
        report.append(
            f"- **Added** FARP {location['name']}{callsign}, shortname `{location['shortname']}`"
        )
        if location.get("elevation_feet") == UNKNOWN_ELEVATION:
            unsurveyed.append(location["name"])

    for key in recycled:
        location = after[key]
        distance = rebuilt[key]
        moved = f"moved {distance:.0f} m" if distance is not None else "previous position unreadable"
        callsign = f" ({location['callsign']})" if location.get("callsign") else ""
        report.append(
            f"- **Rebuilt** FARP {location['name']}{callsign} - {moved}, so an unrelated pad "
            "is reusing the name. Its elevation has been cleared and needs surveying; the "
            f"shortname `{location['shortname']}` still applies."
        )
        if location.get("elevation_feet") == UNKNOWN_ELEVATION:
            unsurveyed.append(location["name"])

    for key in removed:
        report.append(f"- **Removed** FARP {before[key]['name']}")

    for key in common:
        old_flat: dict[str, str] = {}
        new_flat: dict[str, str] = {}
        for field_name in CHANGE_TRACKED_FIELDS:
            flatten(field_name, before[key].get(field_name), old_flat)
            flatten(field_name, after[key].get(field_name), new_flat)
        differences = sorted(
            path
            for path in set(old_flat) | set(new_flat)
            if old_flat.get(path) != new_flat.get(path)
        )
        if not differences:
            continue
        counts["modified"] += 1
        changed_names["modified"].append(after[key]["name"])
        report.append(f"- **Changed** FARP {after[key]['name']}")
        for path in differences:
            report.append(
                f"  - `{path}`: `{old_flat.get(path, '-')}` -> `{new_flat.get(path, '-')}`"
            )

    if unsurveyed:
        report.append("")
        report.append(
            "> Elevation is not published by the map feed. "
            "The following FARPs need `elevation_feet` filled in by hand before merging: "
            + ", ".join(f"**{name}**" for name in unsurveyed)
            + "."
        )

    return report, counts, changed_names


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def commit_subject(counts: dict[str, int], names: dict[str, list[str]]) -> str:
    """A subject line that reads well in `git log` and in release notes."""
    labels = (("added", "add"), ("rebuilt", "rebuild"), ("removed", "remove"), ("modified", "update"))
    total = sum(counts.get(key, 0) for key, _ in labels)
    if total == 0:
        return "Refresh FARP data from live map"

    # Name things while the list is short enough to be worth reading; fall back
    # to counts once it would run past a sensible subject length.
    named = [f"{verb} {', '.join(names[key])}" for key, verb in labels if names.get(key)]
    subject = "Update FARP data: " + "; ".join(named)
    if len(subject) <= 72:
        return subject

    counted = [f"{counts[key]} {key}" for key, _ in labels if counts.get(key)]
    return "Update FARP data: " + ", ".join(counted)


def plain_text(markdown: str) -> str:
    """Strip the markdown emphasis so the same report reads well in a commit."""
    return re.sub(r"[*`]", "", markdown)


def write_github_output(path: str | None, values: dict[str, str]) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            if "\n" in value:
                # A fixed delimiter that appears in the value would let a FARP
                # name inject arbitrary outputs, so derive one that cannot.
                delimiter = f"EOF_{uuid.uuid4().hex}"
                handle.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")
            else:
                handle.write(f"{key}={value}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="update_farps.py",
        description="Rebuild data/farps.toml from the RotorHeads map feed.",
    )
    parser.add_argument(
        "--map-data",
        type=Path,
        default=Path(DEFAULT_MAP_DATA_PATH),
        help=f"map JSON to read, as downloaded by fetch-map-data.sh "
        f"(default: {DEFAULT_MAP_DATA_PATH})",
    )
    parser.add_argument("--farps", type=Path, default=Path(FARPS_PATH))
    parser.add_argument("-o", "--output", type=Path, help="write here instead of in place")
    parser.add_argument(
        "--carry-from",
        type=Path,
        help="take manual fields (elevation, shortname) from this file instead of "
        "--farps; point it at an open pull request's copy of the file",
    )
    parser.add_argument("--report", type=Path, help="write the Markdown change report here")
    parser.add_argument(
        "--commit-message", type=Path, help="write a plain-text commit message here"
    )
    parser.add_argument(
        "--github-output",
        default=os.environ.get("GITHUB_OUTPUT"),
        help="file to append changed/summary outputs to (defaults to $GITHUB_OUTPUT)",
    )
    parser.add_argument("--easting-offset-m", type=float, default=DEFAULT_EASTING_OFFSET_M)
    parser.add_argument(
        "--rebuild-threshold-m",
        type=float,
        default=DEFAULT_REBUILD_THRESHOLD_M,
        help="how far a FARP may move before its name is considered recycled",
    )
    parser.add_argument("--min-farps", type=int, default=DEFAULT_MIN_FARPS)
    parser.add_argument("--dry-run", action="store_true", help="do not write any files")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_arguments(argv)
    level_threshold = (
        logging.DEBUG if args.verbose else logging.ERROR if args.quiet else logging.INFO
    )
    # Level names carry their own colon so the label can be padded as one unit,
    # which lines the messages up in the same column the shell scripts use.
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

    if not args.farps.is_file():
        log.error("FARP file not found: %s", args.farps)
        return 2

    original_text = args.farps.read_text(encoding="utf-8")
    existing = tomllib.loads(original_text).get(FARP_TABLE, [])

    if not args.map_data.is_file():
        log.error(
            "Map data not found: %s. Run scripts/fetch-map-data.sh first.", args.map_data
        )
        return 2

    try:
        feed = json.loads(args.map_data.read_text(encoding="utf-8"))
    except Exception as error:  # malformed or truncated JSON
        log.error("Could not read %s: %s", args.map_data, error)
        return 1

    farps = parse_farps(feed)
    log.info("Parsed %d FARP(s) from the map feed", len(farps))
    if len(farps) < args.min_farps:
        log.error(
            "Only %d FARP(s) in the feed (minimum %d). Refusing to rewrite %s.",
            len(farps),
            args.min_farps,
            args.farps,
        )
        return 1

    for farp in farps:
        resolve_coordinates(farp, args.easting_offset_m)

    carry_source = None
    if args.carry_from and args.carry_from.is_file():
        carry_source = tomllib.loads(args.carry_from.read_text(encoding="utf-8")).get(
            FARP_TABLE, []
        )
        log.debug("Carrying manual FARP fields from %s", args.carry_from)

    updated, rebuilt = merge_locations(
        existing, farps, args.rebuild_threshold_m, carry_source=carry_source
    )
    updated_text = emit(updated, FARP_TABLE)
    report_lines, counts, changed_names = describe_changes(existing, updated, rebuilt)

    changed = updated_text != original_text
    summary = "\n".join(report_lines) if report_lines else "No FARP changes."
    if changed and not report_lines:
        # Formatting-only delta, e.g. the first run after adopting this script.
        summary = "Regenerated `farps.toml`; no FARP data changed."

    log.info(
        "changed=%s added=%d rebuilt=%d removed=%d modified=%d",
        changed,
        counts["added"],
        counts["rebuilt"],
        counts["removed"],
        counts["modified"],
    )
    if not args.quiet:
        print(summary)

    if not args.dry_run:
        destination = args.output or args.farps
        destination.write_text(updated_text, encoding="utf-8")
        if args.report:
            args.report.write_text(summary + "\n", encoding="utf-8")
        if args.commit_message:
            message = commit_subject(counts, changed_names)
            if report_lines:
                message += "\n\n" + plain_text(summary)
            args.commit_message.write_text(message + "\n", encoding="utf-8")

    write_github_output(
        args.github_output,
        {
            "changed": "true" if changed else "false",
            "added": str(counts["added"]),
            "rebuilt": str(counts["rebuilt"]),
            "removed": str(counts["removed"]),
            "modified": str(counts["modified"]),
            "summary": summary,
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
