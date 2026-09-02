#!/usr/bin/env python3
"""
release_notes.py - Write release notes describing what actually changed on the
kneeboard between two versions of the data files.

Commit subjects are listed too, but the useful part is the data diff: a reader
wants to know that a FARP moved, not that a workflow ran.

Usage: release_notes.py [OPTIONS] --current <file>
"""

from __future__ import annotations

import argparse
import logging
import sys
import tomllib
from pathlib import Path
from typing import Any

# Field paths collapse to these labels so the notes read like English rather
# than like a TOML diff. Anything unlisted falls back to its own path.
FIELD_LABELS = {
    "elevation_feet": "Elevation",
    "shortname": "Short name",
    "callsign": "Callsign",
    "icao": "ICAO",
    "navaids.tacan": "TACAN",
    "navaids.vor": "VOR",
    "navaids.ndb": "NDB",
    "navaids.adf": "ADF",
    # Qualified because airports carry both an FM homer and an FM comms
    # frequency, and a bare "FM" on each would be indistinguishable.
    "navaids.fm": "FM Homer",
    "frequencies.hf": "HF",
    "frequencies.fm": "FM",
    "frequencies.vhf": "VHF",
    "frequencies.uhf": "UHF",
}

COORDINATE_PREFIX = "coordinates."
RUNWAY_PREFIX = "runways"

# Emitted in this order for a new location. Matches the order the fields appear
# in the data files, which is also the order they read on the kneeboard.
NAVAID_KEYS = ("tacan", "vor", "ndb", "adf", "fm")
FREQUENCY_KEYS = ("hf", "fm", "vhf", "uhf")

log = logging.getLogger("release_notes")


def load(paths: list[Path] | None) -> dict[str, dict[str, Any]]:
    """Merge both data files into one name-keyed mapping.

    Each entry carries its own `type`, so the sections below can still split
    them apart; loading them together is what keeps the diff logic identical to
    when there was a single file.
    """
    merged: dict[str, dict[str, Any]] = {}
    for path in paths or []:
        if not path.is_file():
            continue
        document = tomllib.loads(path.read_text(encoding="utf-8"))
        for table in ("farp", "airport"):
            for entry in document.get(table, []):
                merged[entry["name"]] = entry
    return merged


def dms_string(location: dict[str, Any]) -> str:
    """The same position in the other format the file stores it in."""
    dms = (location.get("coordinates") or {}).get("dms")
    if not dms:
        return ""
    parts = []
    for axis in ("latitude", "longitude"):
        value = dms.get(axis)
        if not value:
            return ""
        parts.append(
            "{direction} {degrees}\u00b0 {minutes:02d}' {seconds:02d}\"".format(**value)
        )
    return ", ".join(parts)


def mgrs_string(location: dict[str, Any]) -> str:
    grid = (location.get("coordinates") or {}).get("mgrs")
    if not grid:
        return "unknown"
    return "{zone_number} {zone_band} {grid} {easting} {northing}".format(**grid)


def flatten(value: Any, prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            flat.update(flatten(child, f"{prefix}.{key}" if prefix else key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            flat.update(flatten(child, f"{prefix}[{index + 1}]"))
    else:
        flat[prefix] = str(value)
    return flat


def format_value(path: str, value: str | None) -> str:
    if not value:
        return "not yet surveyed" if path == "elevation_feet" else "none"
    if path == "elevation_feet":
        return f"{int(value):,} ft"
    return value


def entry_lines(location: dict[str, Any]) -> list[str]:
    """Everything the file knows about a location that has just appeared.

    Flat rather than grouped, and using the same labels as a diff, so a new
    entry and a changed one read the same way. Fields the location does not have
    are skipped rather than printed empty. Runways collapse to a line each,
    since three sub-bullets for two headings and a length reads worse.
    """
    lines: list[str] = []

    shortname = location.get("shortname") or ""
    if shortname and shortname != str(location["name"]).upper():
        lines.append(f"{FIELD_LABELS['shortname']}: `{shortname}`")
    for key in ("icao", "callsign"):
        if location.get(key):
            lines.append(f"{FIELD_LABELS[key]}: {location[key]}")

    lines.append(
        f"{FIELD_LABELS['elevation_feet']}: "
        f"{format_value('elevation_feet', location.get('elevation_feet'))}"
    )
    lines.append(f"Position: {mgrs_string(location)}")
    coordinates = dms_string(location)
    if coordinates:
        lines.append(f"Coordinates: {coordinates}")

    for table, keys in (("navaids", NAVAID_KEYS), ("frequencies", FREQUENCY_KEYS)):
        values = location.get(table) or {}
        for key in keys:
            if values.get(key):
                lines.append(f"{FIELD_LABELS[f'{table}.{key}']}: {values[key]}")

    for runway in location.get("runways") or []:
        primary = runway.get("primary") or {}
        secondary = runway.get("secondary") or {}
        length = runway.get("length_feet")
        lines.append(
            "Runway {primary}/{secondary}: {length} ft, headings {head}/{tail}".format(
                primary=primary.get("name", "?"),
                secondary=secondary.get("name", "?"),
                length=f"{int(length):,}" if length else "unknown",
                head=primary.get("heading", "?"),
                tail=secondary.get("heading", "?"),
            )
        )
    return lines


def changes_between(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Human-readable bullets for one location that exists in both versions."""
    old, new = flatten(before), flatten(after)
    lines: list[str] = []

    if any(
        old.get(path) != new.get(path)
        for path in set(old) | set(new)
        if path.startswith(COORDINATE_PREFIX)
    ):
        lines.append(f"Position: {mgrs_string(before)} to {mgrs_string(after)}")

    runways_changed = False
    for path in sorted(set(old) | set(new)):
        if old.get(path) == new.get(path):
            continue
        if path.startswith(COORDINATE_PREFIX):
            continue
        if path.startswith(RUNWAY_PREFIX):
            runways_changed = True
            continue
        if path == "name":
            continue
        label = FIELD_LABELS.get(path, path)
        lines.append(f"{label}: {format_value(path, old.get(path))} to {format_value(path, new.get(path))}")

    if runways_changed:
        lines.append("Runway details updated")
    return lines


def section(title: str, kind: str, before: dict, after: dict) -> list[str]:
    names = {
        name
        for name in set(before) | set(after)
        if (before.get(name) or after.get(name)).get("type") == kind
    }

    groups: list[tuple[str, list[str]]] = []

    added = [
        [f"- {name}"] + [f"  - {line}" for line in entry_lines(after[name])]
        for name in sorted(name for name in names if name not in before)
    ]
    groups.append(("New", [line for entry in added for line in entry]))

    updated: list[str] = []
    for name in sorted(name for name in names if name in before and name in after):
        bullets = changes_between(before[name], after[name])
        if not bullets:
            continue
        updated.append(f"- {name}")
        updated.extend(f"  - {bullet}" for bullet in bullets)
    groups.append(("Updated", updated))

    groups.append(
        ("Removed", [f"- {name}" for name in sorted(name for name in names if name not in after)])
    )

    body: list[str] = []
    for heading, lines in groups:
        if lines:
            body += [f"**{heading}**", ""] + lines + [""]

    if not body:
        return []
    return [f"### {title}", ""] + body


def table(heading: str, columns: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return []
    separator = "|" + " --- |" * len(columns)
    header = "| " + " | ".join(columns) + " |"
    body = ["| " + " | ".join(row) + " |" for row in rows]
    return [f"### {heading}", "", header, separator] + body + [""]


def short_name(farp: dict[str, Any]) -> str:
    """Blank when the shortname is just the name, matching the kneeboard.

    A column of `ADAMA | ADAMA` carries no information, and printing it here
    while the kneeboard omits it would suggest the two disagree.
    """
    shortname = farp.get("shortname") or ""
    if not shortname or shortname == str(farp["name"]).upper():
        return "-"
    return f"`{shortname}`"


def roster(after: dict[str, Any]) -> list[str]:
    """A full listing of both kinds of location, not just the changed ones.

    This makes a release page usable as a standalone reference. Frequencies and
    elevations are deliberately absent: those belong on the kneeboard, and a
    second copy here would only go stale.
    """

    def of_kind(kind: str) -> list[dict[str, Any]]:
        return sorted(
            (entry for entry in after.values() if entry.get("type") == kind),
            key=lambda entry: entry["name"],
        )

    farps = table(
        "FARPs in this release",
        ["FARP", "Short", "Callsign", "MGRS"],
        [
            [
                farp["name"],
                short_name(farp),
                farp.get("callsign", "-"),
                mgrs_string(farp),
            ]
            for farp in of_kind("farp")
        ],
    )
    airports = table(
        "Airports in this release",
        ["Airport", "ICAO", "MGRS"],
        [
            [airport["name"], airport.get("icao", "-"), mgrs_string(airport)]
            for airport in of_kind("airport")
        ],
    )
    return farps + airports


def build_notes(
    before: dict[str, Any],
    after: dict[str, Any],
    commits: list[str],
    previous_tag: str | None,
) -> str:
    lines: list[str] = []

    data_sections = section("FARPs", "farp", before, after) + section(
        "Airports", "airport", before, after
    )

    if not before:
        lines += ["## First release", ""]
    elif data_sections:
        lines += [f"## What changed since {previous_tag}" if previous_tag else "## What changed", ""]
        lines += data_sections
    else:
        lines += ["## What changed", "", "No changes to the location data.", ""]

    lines += roster(after)

    if commits:
        lines += [
            "<details>",
            "<summary>Commits in this release</summary>",
            "",
        ]
        lines += [f"- {subject}" for subject in commits]
        lines += ["", "</details>", ""]

    return "\n".join(lines).rstrip() + "\n"


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="release_notes.py", description="Write kneeboard release notes."
    )
    parser.add_argument(
        "--current", type=Path, nargs="+", required=True, help="new data files"
    )
    parser.add_argument(
        "--previous", type=Path, nargs="+", help="data files from the previous release"
    )
    parser.add_argument("--previous-tag", help="name of the previous release, for the heading")
    parser.add_argument("--commits", type=Path, help="file of commit subjects, one per line")
    parser.add_argument("-o", "--output", type=Path, help="write here instead of stdout")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_arguments(argv)
    level_threshold = logging.INFO
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

    missing = [path for path in args.current if not path.is_file()]
    if missing:
        log.error("File(s) not found: %s", ", ".join(str(p) for p in missing))
        return 2

    commits = []
    if args.commits and args.commits.is_file():
        commits = [
            line.strip()
            for line in args.commits.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    notes = build_notes(load(args.previous), load(args.current), commits, args.previous_tag)
    if args.output:
        args.output.write_text(notes, encoding="utf-8")
    else:
        sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
