# RotorHeads Kneeboards

This repo contains the files needed to build the HTML pages of the kneeboards
that I use for the DCS RotorHeads server.
The files are built with a tool called [gomplate](https://gomplate.ca/).

## I just want the files!

Download the latest version of the kneeboard files from the [latest release](https://github.com/tritiumgg/rotorheads-kneeboards/releases).

## About the data

FARP information comes straight from the server's live map and updates on its
own. Airport information is entered by hand.

**Coordinates.** The live map draws FARP markers about 100 m east of the real
pad. The coordinates on the kneeboard are corrected, so they match what you see
in the cockpit.

**Elevations.** The live map does not report FARP elevation, so it has to be
looked up in game. A FARP that has just been built may show a blank elevation
until someone gets round to it. Airport elevations come from the terrain itself.

**Airports.** Only airfields inside the mission's current playable area are
shown, and that area moves as the campaign does. Runway headings are magnetic,
and elevations and runway lengths are what the sim models rather than what the
real-world charts say.

**Short names.** Each FARP has a short name of up to six characters, so it can be
typed into an aircraft nav system. Most are just the name in capitals. Longer
names have letters dropped until they fit:

| Name | Short name |
| --- | --- |
| HAWK | HAWK |
| APOLLO | APOLLO |
| SHIELDS | SHILDS |
| WHIRLWIND | WRLWND |
| VINTOKRYL | VNTKRL |

The kneeboard only prints a short name when it differs from the name, so most
FARPs just show the name. It sits in the same column as an airport's ICAO code.

If a FARP is torn down and a new one is built somewhere else under the same name,
its elevation is cleared and has to be looked up again. The short name stays the
same, since it only depends on the name.

Crate counts and FARP status are not on the kneeboard.

## Generating the data yourself

Both data files are generated, from three different sources:

- `data/farps.toml` comes entirely from the live map feed, nightly.
- `data/airports.toml` comes mostly from a **terrain dump**, taken in game with
  `scripts/dcs/dump_airbases_hook.lua` and imported with
  `scripts/import_airports.py`. Re-run that after a DCS patch, not nightly.
- The nightly job owns exactly two things on airports: `navaids.adf` and
  `navaids.fm`, from the mission's logistics beacons, and `display`, from
  whether the airfield is inside the mission's playable boundary polygon.

Because `display` is computed, editing it by hand will not survive the next run.
An airfield outside the boundary keeps all its data and simply stops appearing
on the kneeboard.

You will need:

- [Python](https://www.python.org/) 3.11 or newer, for the scripts
- [gomplate](https://gomplate.ca/), to build the pages
- [just](https://just.systems/), optional, for the shortcuts below
- Google Chrome and [poppler](https://poppler.freedesktop.org/), only if you want
  to build the PDF and PNGs locally

If you use [mise](https://mise.jdx.dev/), the first three are pinned in
`mise.toml` and `mise install` sets them up, along with a `.venv` that activates
whenever you are in the repo. Actions uses the same file, so the versions you
build with locally are the versions that build the releases. Put the map data
address in `mise.local.toml`, which is gitignored:

```toml
[env]
FARP_MAP_URL = "http://.../mapdata/map.json"
```

### With just

```
just install         # install the Python packages
just update-data     # download the map data and rewrite the FARP entries
just lint            # check the data over
just build           # build the pages into build/
```

`just all` does all four in one go. Anything that reaches the server needs the
feed's address, so set `FARP_MAP_URL` in your shell or pass it as
`just map_url=... update-data`.

`just --list` shows everything else, including `preview-data` for a no-op run,
`test` for a run against the test fixture, `export` for the PDF and PNGs,
`release-notes` for a preview of the notes, and `watch` for rebuilding as you
edit.

### Without just

```
pip install -r scripts/requirements.txt
./scripts/fetch-map-data.sh --force <map data url>
./scripts/update_farps.py
./scripts/update_airports.py
./scripts/lint_data.py
gomplate
```

`fetch-map-data.sh` is the only thing that talks to the server, and it has no
address built in, so pass the feed's url as an argument or set `FARP_MAP_URL`.
It writes to `build/map.json` and everything else reads that file, so a run can
always be repeated on exactly the same input.

All the scripts take `--help`. `update_farps.py` also takes `--dry-run` to write
nothing, `--output` to write elsewhere, `--map-data` to read a different file,
and `--verbose`. `tests/fixtures/map.json` is hand-built test data rather
than a copy of the live server, so you can exercise the whole pipeline without
it:

```
./scripts/update_farps.py --map-data tests/fixtures/map.json --dry-run
```

Its eight FARPs cover every shortname rule, both UTM zones the map spans, and
every kind of marker the parser has to ignore. Extend it when you hit a case it
does not cover; a live capture from `./scripts/fetch-map-data.sh` is a useful
starting point, but do not simply overwrite the fixture with one.

The pages end up in `build/`.

### Things worth knowing before you change anything

The 100 m eastward offset in the map feed was confirmed against the ALPHA, BRAVO
and CHARLIE spawn points and against every hand-entered coordinate in the file.
`DEFAULT_EASTING_OFFSET_M` turns it off if it ever stops being true. Changing it
moves every FARP at once, which will look like every FARP was rebuilt.

Elevation is empty rather than `"0"` for an unsurveyed FARP, because a FARP could
genuinely sit at 0 ft. `lint_data.py` fails on an empty one.

Short names are worked out once and then carried forward, so editing one by hand
sticks. Clashes are resolved by replacing the last character with a number.
`SHORTNAME_MAX_LENGTH` is six in both scripts and has to be changed in both.

Airport runway headings are converted from the terrain's true bearings using the
World Magnetic Model at `DEFAULT_EPOCH`. DCS models its own variation, which may
differ by a fraction of a degree; that is well inside the precision a heading is
read to, but it is why the numbers can differ by one from a published chart.

`scripts/locations_toml.py` holds the schema and the writer that both files
share, which is what lets the linter check a hand-edited airport against the
same formatting rules the generator follows.

Elevation and short name are the only fields carried forward between runs;
everything else is rewritten from the feed each time, so nothing else can go
stale. A FARP more than `DEFAULT_REBUILD_THRESHOLD_M` (50 m) from where it was is
treated as a different FARP reusing the name, and loses the fields listed in
`POSITION_DEPENDENT_FIELDS`. Add any future hand-maintained field to
`CARRIED_FIELDS`, and to `POSITION_DEPENDENT_FIELDS` too if it describes the pad
rather than the name.

Crate count and status are parsed on every run but not written out. Add them to
`EXTRA_EMITTED_FIELDS` and to `src/comms-nav.html` if you want them.

FARP data is refreshed on a schedule. GitHub disables scheduled workflows in a
repository that has gone 60 days without a commit, so if the data ever looks
stale, check whether the workflow is still enabled before looking anywhere else.
Any push or a manual run turns it back on.
