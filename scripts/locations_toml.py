"""
locations_toml.py - Shared reading and writing for data/farps.toml and
data/airports.toml.

Both files use the same schema; they differ only in which fields apply and in
which generator owns them. Keeping the emitter in one place is what lets the
linter check that a hand-edited airport is still in canonical form, using
exactly the code that writes the generated FARPs.
"""

from __future__ import annotations

from typing import Any

FARPS_PATH = "data/farps.toml"
AIRPORTS_PATH = "data/airports.toml"

# Top-level array name in each file.
FARP_TABLE = "farp"
AIRPORT_TABLE = "airport"

# Sentinel elevation for a FARP that has never been surveyed in-game. Empty
# rather than "0", because a FARP genuinely at 0 ft MSL is possible and must not
# be indistinguishable from missing data.
UNKNOWN_ELEVATION = ""

ENTRY_KEY_ORDER = (
    "type",
    "name",
    "shortname",
    "callsign",
    "icao",
    "display",
    "elevation_feet",
    "crates",
    "status",
)
NAVAID_KEY_ORDER = ("tacan", "vor", "ndb", "adf", "fm")
FREQUENCY_KEY_ORDER = ("hf", "fm", "vhf", "uhf")
RUNWAY_KEY_ORDER = ("elevation_feet", "length_feet", "primary", "secondary")
DMS_KEY_ORDER = ("direction", "degrees", "minutes", "seconds")
MGRS_KEY_ORDER = ("zone_number", "zone_band", "grid", "easting", "northing")
RUNWAY_END_KEY_ORDER = ("name", "heading")


def format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return '"{}"'.format(str(value).replace("\\", "\\\\").replace('"', '\\"'))


def ordered_keys(table: dict[str, Any], key_order: tuple[str, ...]) -> list[str]:
    known = [key for key in key_order if key in table]
    extra = [key for key in table if key not in key_order]
    return known + extra


def format_inline_table(table: dict[str, Any], key_order: tuple[str, ...]) -> str:
    body = ", ".join(
        f"{key} = {format_value(table[key])}" for key in ordered_keys(table, key_order)
    )
    return "{ " + body + " }"


def emit_key_values(
    lines: list[str],
    table: dict[str, Any],
    key_order: tuple[str, ...],
    comments: dict[str, str] | None = None,
) -> None:
    comments = comments or {}
    for key in ordered_keys(table, key_order):
        if isinstance(table[key], (dict, list)):
            continue
        lines.append(f"{key} = {format_value(table[key])}{comments.get(key, '')}")


def emit_entry(entry: dict[str, Any], table: str) -> list[str]:
    lines: list[str] = []
    heading = entry["name"]
    if entry.get("type") == "farp":
        heading = f"FARP {heading}"
    lines.append(f"# {heading}")
    lines.append("")
    lines.append(f"[[{table}]]")

    comments = {}
    if entry.get("type") == "farp" and entry.get("elevation_feet") == UNKNOWN_ELEVATION:
        comments["elevation_feet"] = "  # TODO: survey elevation in-game"
    emit_key_values(lines, entry, ENTRY_KEY_ORDER, comments=comments)

    coordinates = entry.get("coordinates", {})
    if "dms" in coordinates:
        lines.append("")
        lines.append(f"[{table}.coordinates.dms]")
        for axis in ("latitude", "longitude"):
            if axis in coordinates["dms"]:
                inline = format_inline_table(coordinates["dms"][axis], DMS_KEY_ORDER)
                lines.append(f"{axis} = {inline}")
    if "mgrs" in coordinates:
        lines.append("")
        lines.append(f"[{table}.coordinates.mgrs]")
        emit_key_values(lines, coordinates["mgrs"], MGRS_KEY_ORDER)

    if entry.get("navaids"):
        lines.append("")
        lines.append(f"[{table}.navaids]")
        emit_key_values(lines, entry["navaids"], NAVAID_KEY_ORDER)

    if entry.get("frequencies"):
        lines.append("")
        lines.append(f"[{table}.frequencies]")
        emit_key_values(lines, entry["frequencies"], FREQUENCY_KEY_ORDER)

    for runway in entry.get("runways", []):
        lines.append("")
        lines.append(f"[[{table}.runways]]")
        emit_key_values(lines, runway, RUNWAY_KEY_ORDER)
        for end in ("primary", "secondary"):
            if end in runway:
                lines.append(f"{end} = {format_inline_table(runway[end], RUNWAY_END_KEY_ORDER)}")

    return lines


def emit(entries: list[dict[str, Any]], table: str) -> str:
    """Render a whole file.

    Names are upper-cased here rather than where they are read, so the rule
    covers hand-written airports as well as generated FARPs, and the linter's
    canonical-format check catches any that slip through. Sorting happens after,
    or the order would depend on the case the name arrived in.
    """
    normalised = sorted(
        ({**entry, "name": str(entry["name"]).upper()} for entry in entries),
        key=lambda entry: entry["name"],
    )
    blocks = [emit_entry(entry, table) for entry in normalised]
    return "\n\n".join("\n".join(block) for block in blocks) + "\n"
