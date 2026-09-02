#!/usr/bin/env python3
"""
build_route.py - Write a DCS Route Tool preset containing every FARP.

The preset is meant to be loaded before spawn and then pulled apart: each
waypoint is named with the FARP's shortname, so they work as air control points
to build a real flight plan from inside the cockpit.

Usage: build_route.py [OPTIONS]
"""

from __future__ import annotations

import argparse
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
    from pyproj import CRS, Transformer
except ModuleNotFoundError as _error:
    raise _missing_dependency(_error) from None

sys.path.insert(0, str(Path(__file__).resolve().parent))

from locations_toml import FARP_TABLE, FARPS_PATH

DEFAULT_OUTPUT_PATH = "build/Afghanistan.lua"
DEFAULT_PRESET_NAME = "RotorHeads_FARPs"

# Derived from coord.LOtoLL/LLtoLO samples taken in-game, then fitted: both
# offsets land on round whole numbers and every sample round-trips to under a
# centimetre, which is what confirms these are the values the map actually uses.
# Neighbouring central meridians are wrong by 9 km or more.
AFGHANISTAN = {
    "central_meridian": 63,
    "false_easting": -300150.0,
    "false_northing": -3759657.0,
    "scale_factor": 0.9996,
}

FEET_PER_METRE = 3.280839895

# Height above each FARP, in metres, when no flat altitude is given. Field
# elevations here span 2,300 to 9,500 ft, so a single low cruise altitude would
# put waypoints underground at the higher pads.
DEFAULT_AGL_BUFFER_M = 500.0

log = logging.getLogger("build_route")


def projection(parameters: dict[str, Any]) -> Transformer:
    """lat/lon to DCS x (north) and z (east), in metres."""
    crs = CRS.from_proj4(
        " ".join(
            [
                "+proj=tmerc",
                "+lat_0=0",
                f"+lon_0={parameters['central_meridian']}",
                f"+k_0={parameters['scale_factor']}",
                f"+x_0={parameters['false_easting']}",
                f"+y_0={parameters['false_northing']}",
                "+towgs84=0,0,0,0,0,0,0",
                "+units=m",
                "+vunits=m",
                "+ellps=WGS84",
                "+no_defs",
                # north-east-up, so transform() returns (x, z) in DCS's order
                "+axis=neu",
            ]
        )
    )
    return Transformer.from_crs(CRS("WGS84"), crs)


_MGRS = mgrs.MGRS()


def decimal_degrees(dms: dict[str, Any]) -> float:
    value = dms["degrees"] + dms["minutes"] / 60 + dms["seconds"] / 3600
    return -value if dms["direction"] in ("S", "W") else value


def position(location: dict[str, Any]) -> tuple[float, float] | None:
    """Read the MGRS in preference to the DMS.

    Both describe the same point, but DMS is stored to whole seconds, which is
    worth up to ~20 m here. MGRS is stored to the metre.
    """
    coordinates = location.get("coordinates") or {}
    grid = coordinates.get("mgrs")
    if grid:
        try:
            return _MGRS.toLatLon(
                "{zone_number}{zone_band}{grid}{easting}{northing}".format(**grid)
            )
        except Exception:
            log.warning("%s has unreadable MGRS; falling back to DMS", location["name"])

    dms = coordinates.get("dms")
    if not dms:
        return None
    return decimal_degrees(dms["latitude"]), decimal_degrees(dms["longitude"])


def waypoints(
    locations: list[dict[str, Any]],
    transformer: Transformer,
    agl_buffer_m: float,
    flat_altitude_m: float | None,
) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for location in sorted(locations, key=lambda item: item["name"]):
        if location.get("type") != "farp":
            continue

        coordinates = position(location)
        if coordinates is None:
            log.warning("Skipping %s: no coordinates", location["name"])
            continue
        x, z = transformer.transform(*coordinates)

        elevation = location.get("elevation_feet")
        if flat_altitude_m is not None:
            altitude = flat_altitude_m
        elif elevation:
            altitude = int(elevation) / FEET_PER_METRE + agl_buffer_m
        else:
            altitude = agl_buffer_m
            log.warning(
                "%s has no elevation; using %.0f m, which may be below ground",
                location["name"],
                altitude,
            )

        points.append(
            {
                # The Route Tool shows this in the cockpit, so the shortname is
                # what makes these usable as named waypoints.
                "name": location.get("shortname") or location["name"],
                "x": x,
                "y": z,
                "alt": round(altitude),
            }
        )
    return points


def render_preset(name: str, points: list[dict[str, Any]]) -> str:
    """Reproduce the Route Tool's own formatting, tabs and end-of-block comments."""
    lines = [f'\t["{name}"] = ', "\t{"]
    for index, point in enumerate(points, start=1):
        lines += [
            f"\t\t[{index}] = ",
            "\t\t{",
            f'\t\t\t["alt"] = {point["alt"]},',
            '\t\t\t["type"] = "Turning Point",',
            '\t\t\t["ETA"] = 0,',
            '\t\t\t["ETA_locked"] = false,',
            f'\t\t\t["y"] = {point["y"]:.6f},',
            f'\t\t\t["x"] = {point["x"]:.6f},',
            f'\t\t\t["name"] = "{point["name"]}",',
            '\t\t\t["action"] = "Turning Point",',
            '\t\t\t["alt_type"] = "BARO",',
            '\t\t\t["speed_locked"] = false,',
            f"\t\t}}, -- end of [{index}]",
        ]
    lines += [f'\t}}, -- end of ["{name}"]']
    return "\n".join(lines)


def merge(existing: str | None, name: str, preset: str) -> str:
    """Replace just this preset, leaving any other saved routes untouched."""
    if not existing or "presets" not in existing:
        return "presets = \n{\n" + preset + "\n} -- end of presets\n"

    marker = re.escape(f'}}, -- end of ["{name}"]')
    block = re.compile(
        r"\t\[\"" + re.escape(name) + r"\"\] = \n.*?" + marker + "\n",
        re.DOTALL,
    )
    if block.search(existing):
        log.debug("Replacing existing preset %s", name)
        return block.sub(preset + "\n", existing, count=1)

    closing = re.compile(r"\n\} -- end of presets")
    if not closing.search(existing):
        raise ValueError("Could not find the end of the presets table")
    log.debug("Appending preset %s", name)
    return closing.sub("\n" + preset + "\n} -- end of presets", existing, count=1)


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="build_route.py",
        description="Write a DCS Route Tool preset containing every FARP.",
    )
    parser.add_argument("--farps", type=Path, default=Path(FARPS_PATH))
    parser.add_argument("-o", "--output", type=Path, default=Path(DEFAULT_OUTPUT_PATH))
    parser.add_argument(
        "--merge-into",
        type=Path,
        help="existing Route Tool file to fold this preset into, keeping the "
        "other saved routes. The result still goes to --output.",
    )
    parser.add_argument("--preset-name", default=DEFAULT_PRESET_NAME)
    parser.add_argument(
        "--agl-buffer-m",
        type=float,
        default=DEFAULT_AGL_BUFFER_M,
        help=f"height above each FARP (default: {DEFAULT_AGL_BUFFER_M:.0f})",
    )
    parser.add_argument(
        "--flat-altitude-m",
        type=float,
        help="use one altitude for every waypoint instead of following the terrain",
    )
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

    if not args.farps.is_file():
        log.error("FARP file not found: %s", args.farps)
        return 2

    locations = tomllib.loads(args.farps.read_text(encoding="utf-8")).get(FARP_TABLE, [])
    points = waypoints(
        locations, projection(AFGHANISTAN), args.agl_buffer_m, args.flat_altitude_m
    )
    if not points:
        log.error("No FARPs found in %s", args.farps)
        return 1

    existing = None
    if args.merge_into and args.merge_into.is_file():
        existing = args.merge_into.read_text(encoding="utf-8")
    elif args.merge_into:
        log.warning("%s does not exist; writing a new file", args.merge_into)

    try:
        content = merge(existing, args.preset_name, render_preset(args.preset_name, points))
    except ValueError as error:
        log.error("Could not merge into %s: %s", args.merge_into, error)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(content, encoding="utf-8")
    log.info(
        "Wrote %d waypoints as preset %s to %s", len(points), args.preset_name, args.output
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
