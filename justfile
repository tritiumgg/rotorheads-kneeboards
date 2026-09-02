# Address of the live map data feed. Override with `just map_url=... fetch`,
# or set FARP_MAP_URL in your shell.
map_url := env_var_or_default("FARP_MAP_URL", "")

# Show the available commands
default:
    just --list

# Install the Python packages the scripts need
install:
    #!/usr/bin/env bash
    set -euo pipefail

    # `python -m pip` rather than `pip`, so the packages always land in the
    # interpreter that will run the scripts rather than whichever pip happens to
    # be first on PATH. uv-created virtualenvs have no pip at all, hence the
    # fallback.
    if python -m pip --version >/dev/null 2>&1; then
        python -m pip install -r scripts/requirements.txt
    elif command -v uv >/dev/null 2>&1; then
        uv pip install -r scripts/requirements.txt
    else
        echo "Neither pip nor uv is available to $(command -v python)" >&2
        exit 1
    fi

# Download live map data to build/map.json
fetch-map:
    #!/usr/bin/env bash
    set -euo pipefail
    ./scripts/fetch-map-data.sh --force --verbose {{ quote(map_url) }}

# Rewrite the parts of the data files that the live map feed owns
update-data: fetch-map
    #!/usr/bin/env bash
    set -euo pipefail
    ./scripts/update_farps.py --verbose
    ./scripts/update_airports.py --verbose

# Show what live map data would change, without writing anything
preview-data: fetch-map
    #!/usr/bin/env bash
    set -euo pipefail
    ./scripts/update_farps.py --dry-run --verbose
    ./scripts/update_airports.py --dry-run --verbose

# Run the generators against the test fixture, without writing anything
test:
    #!/usr/bin/env bash
    set -euo pipefail
    ./scripts/update_farps.py --map-data tests/fixtures/map.json --dry-run --verbose
    ./scripts/update_airports.py --map-data tests/fixtures/map.json --dry-run --verbose

# Rebuild airports.toml from a terrain dump taken with dump_airbases_hook.lua
import-airports dump:
    #!/usr/bin/env bash
    set -euo pipefail
    ./scripts/import_airports.py --verbose {{ quote(dump) }}

# Check the location data for anything missing or inconsistent
lint:
    #!/usr/bin/env bash
    set -euo pipefail
    ./scripts/lint_data.py

# Build a DCS Route Tool preset with every FARP on it
route preset_file="":
    #!/usr/bin/env bash
    set -euo pipefail
    args=(--verbose)
    if [[ -n "{{ preset_file }}" ]]; then
        args+=(--merge-into "{{ preset_file }}")
    fi
    ./scripts/build_route.py "${args[@]}"

# Build the kneeboard pages into build/
build:
    #!/usr/bin/env bash
    set -euo pipefail
    gomplate

# Rebuild the pages whenever a file changes
watch:
    #!/usr/bin/env bash
    set -euo pipefail
    watchexec -i "build/**" -- gomplate

# Fetch, update the data, check it, and rebuild the pages
all: update-data lint build

# Render the pages to the PDF and PNGs that the release publishes
export: build
    #!/usr/bin/env bash
    set -euo pipefail

    # Chrome is not on PATH on macOS, so look in the usual place before giving up.
    chrome="${CHROME:-}"
    if [[ -z "${chrome}" ]]; then
        for candidate in \
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
            "google-chrome" \
            "chromium"; do
            if [[ -x "${candidate}" ]] || command -v "${candidate}" >/dev/null 2>&1; then
                chrome="${candidate}"
                break
            fi
        done
    fi
    if [[ -z "${chrome}" ]]; then
        echo "Could not find Chrome. Set CHROME to its path and try again." >&2
        exit 1
    fi
    if ! command -v pdftoppm >/dev/null 2>&1; then
        echo "pdftoppm is missing. Install poppler (brew install poppler)." >&2
        exit 1
    fi

    mkdir -p build/png
    # --virtual-time-budget: wait for the webfont before printing
    "${chrome}" \
        --headless=new \
        --disable-gpu \
        --virtual-time-budget=10000 \
        --no-pdf-header-footer \
        --print-to-pdf=build/rotorheads_comms-and-nav-reference.pdf \
        build/comms-nav.html
    pdftoppm -png \
        build/rotorheads_comms-and-nav-reference.pdf \
        build/png/rotorheads_comms-and-nav-reference
    echo "Wrote build/rotorheads_comms-and-nav-reference.pdf and build/png/"

# Preview the release notes for everything since the last release
release-notes:
    #!/usr/bin/env bash
    set -euo pipefail

    commits="$(mktemp)"
    previous_data="$(mktemp -d)"
    trap 'rm -rf "${commits}" "${previous_data}"' EXIT

    previous="$(git tag --list 'v*' --sort=-v:refname | head -n 1)"
    args=(--current data/farps.toml data/airports.toml --commits "${commits}")
    if [[ -n "${previous}" ]]; then
        git log --no-merges --pretty=format:'%s' "${previous}..HEAD" > "${commits}"
        mkdir -p "${previous_data}"
        for file in farps airports; do
            git show "${previous}:data/${file}.toml" > "${previous_data}/${file}.toml" 2>/dev/null || true
        done
        args+=(--previous "${previous_data}/farps.toml" "${previous_data}/airports.toml"
               --previous-tag "${previous}")
    fi
    ./scripts/release_notes.py "${args[@]}"

# Delete the build output
clean:
    #!/usr/bin/env bash
    set -euo pipefail
    rm -rf build
