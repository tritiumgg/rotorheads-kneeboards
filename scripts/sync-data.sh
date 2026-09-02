#!/usr/bin/env bash
#
# sync-data.sh - Refresh the parts of data/farps.toml and data/airports.toml that
# the RotorHeads live map feed owns, and manage the resulting pull request.
#
# Opens a PR when the regenerated file differs from the base branch. If a PR
# from this automation is already open and its contents no longer match the
# freshly generated file, that PR is closed and replaced.
#
# Usage: sync-data.sh [OPTIONS]
#

set -euo pipefail
IFS=$'\n\t'

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
readonly REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

readonly FARPS_FILE="data/farps.toml"
readonly AIRPORTS_FILE="data/airports.toml"
readonly DATA_FILES=("data/farps.toml" "data/airports.toml")
readonly PR_LABEL="farp-data"

BRANCH="${FARP_BRANCH:-automation/data}"
MAP_DATA="${FARP_MAP_DATA:-build/map.json}"
DRY_RUN=0
QUIET=0
VERBOSE=0

REPORT_FILE=""
AIRPORT_REPORT_FILE=""
COMMIT_MESSAGE_FILE=""
PR_FARPS_FILE=""
PR_AIRPORTS_FILE=""
STARTING_BRANCH=""

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log_error() {
    printf '[%s] %-8s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "ERROR:" "$*" >&2
}

log_warning() {
    [[ ${QUIET} -eq 1 ]] && return 0
    printf '[%s] %-8s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "WARNING:" "$*"
}

log_info() {
    [[ ${QUIET} -eq 1 ]] && return 0
    printf '[%s] %-8s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "INFO:" "$*"
}

log_debug() {
    [[ ${QUIET} -eq 1 ]] && return 0
    [[ ${VERBOSE} -eq 0 ]] && return 0
    printf '[%s] %-8s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "DEBUG:" "$*"
}

cleanup() {
    [[ -n "${REPORT_FILE}" && -f "${REPORT_FILE}" ]] && rm -f "${REPORT_FILE}"
    [[ -n "${AIRPORT_REPORT_FILE}" && -f "${AIRPORT_REPORT_FILE}" ]] && rm -f "${AIRPORT_REPORT_FILE}"
    [[ -n "${COMMIT_MESSAGE_FILE}" && -f "${COMMIT_MESSAGE_FILE}" ]] && rm -f "${COMMIT_MESSAGE_FILE}"
    [[ -n "${PR_FARPS_FILE}" && -f "${PR_FARPS_FILE}" ]] && rm -f "${PR_FARPS_FILE}"
    [[ -n "${PR_AIRPORTS_FILE}" && -f "${PR_AIRPORTS_FILE}" ]] && rm -f "${PR_AIRPORTS_FILE}"
    # Leave a local checkout where we found it; CI gets a fresh clone anyway.
    if [[ -n "${STARTING_BRANCH}" && "$(git branch --show-current)" != "${STARTING_BRANCH}" ]]; then
        git switch --quiet "${STARTING_BRANCH}" 2>/dev/null || true
    fi
    return 0
}
trap cleanup EXIT

usage() {
    cat <<EOF
Usage: ${SCRIPT_NAME} [OPTIONS]

Regenerate FARP data in ${FARPS_FILE} and open or replace a pull request.

Options:
      --branch <name>   Head branch for the automated PR (default: ${BRANCH})
      --map-data <file> Map JSON to read (default: ${MAP_DATA}). Download it
                        first with scripts/fetch-map-data.sh
      --dry-run         Show what would change without writing, pushing or
                        touching any pull request
  -q, --quiet           Suppress all output except errors
  -v, --verbose         Enable verbose output
  -h, --help            Display this help message

Examples:
  # Normal run, as invoked by the scheduled workflow
  ${SCRIPT_NAME}

  # See what the feed would change without touching the repo
  ${SCRIPT_NAME} --dry-run --verbose

Environment:
  GH_TOKEN              Token used by gh for pull request operations
  FARP_BRANCH           Default head branch name
  FARP_MAP_DATA         Default map JSON path
EOF
}

parse_arguments() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --branch)
                BRANCH="$2"
                shift 2
                ;;
            --map-data)
                MAP_DATA="$2"
                shift 2
                ;;
            --dry-run)
                DRY_RUN=1
                shift
                ;;
            -q|--quiet)
                QUIET=1
                shift
                ;;
            -v|--verbose)
                VERBOSE=1
                QUIET=0
                shift
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                log_error "Unknown option: $1"
                usage
                exit 2
                ;;
        esac
    done
}

run_command() {
    if [[ ${DRY_RUN} -eq 1 ]]; then
        log_info "[DRY RUN] Would execute: $*"
    else
        log_debug "Executing: $*"
        "$@"
    fi
}

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        log_error "Required command not found: $1"
        exit 127
    fi
}

# ---------------------------------------------------------------------------
# Pull request helpers
# ---------------------------------------------------------------------------

# Number of the open automation PR, or empty string.
open_pr_number() {
    gh pr list --head "${BRANCH}" --state open --json number \
        --jq 'if length > 0 then .[0].number else "" end' 2>/dev/null || true
}

close_pr() {
    local number="$1"
    local reason="$2"

    log_info "Closing stale pull request #${number}"
    run_command gh pr close "${number}" --delete-branch --comment "${reason}"
}

# Copy the open PR's versions of the data files, so the generators can carry
# manual fields across from them and the result can be compared against them.
fetch_pr_data() {
    if ! git fetch --quiet origin "${BRANCH}" 2>/dev/null; then
        log_warning "Could not fetch ${BRANCH}; treating the open pull request as empty"
        return 1
    fi

    PR_FARPS_FILE="$(mktemp)"
    if ! git show "FETCH_HEAD:${FARPS_FILE}" >"${PR_FARPS_FILE}" 2>/dev/null; then
        log_warning "${BRANCH} has no ${FARPS_FILE}"
        rm -f "${PR_FARPS_FILE}"
        PR_FARPS_FILE=""
    fi

    PR_AIRPORTS_FILE="$(mktemp)"
    if ! git show "FETCH_HEAD:${AIRPORTS_FILE}" >"${PR_AIRPORTS_FILE}" 2>/dev/null; then
        log_warning "${BRANCH} has no ${AIRPORTS_FILE}"
        rm -f "${PR_AIRPORTS_FILE}"
        PR_AIRPORTS_FILE=""
    fi

    [[ -n "${PR_FARPS_FILE}" || -n "${PR_AIRPORTS_FILE}" ]]
}

# A pull request whose base has drifted far enough to conflict cannot be merged,
# so it has to be replaced even when its FARP data is still current.
pr_has_conflicts() {
    local number="$1"
    local state

    state="$(gh pr view "${number}" --json mergeable --jq '.mergeable' 2>/dev/null || echo UNKNOWN)"
    log_debug "Pull request #${number} mergeable state: ${state}"
    [[ "${state}" == "CONFLICTING" ]]
}

build_pr_body() {
    local base_sha="$1"

    cat <<EOF
Automated update of the data the live map feed owns.

## FARPs

$(cat "${REPORT_FILE}")

## Airports

$(cat "${AIRPORT_REPORT_FILE}")

## What to do with this

Commit any missing \`elevation_feet\` values straight to this branch. The feed
does not publish elevation, so it has to be surveyed in game, and \`Lint Data\`
fails while one is empty. A corrected \`shortname\` can go the same way.

If the live map changes before you merge, this pull request is closed and a new
one opened in its place. Elevations and shortnames you have committed here are
carried into it; the commit itself is not. **Anything else you change on this
branch is lost when that happens**, so make other edits, including airport
entries, in a separate pull request. Those merge alongside this one without
conflicting.

<details>
<summary>How this was generated</summary>

- Read from the live map feed at $(date -u '+%Y-%m-%d %H:%M UTC').
- Built by \`scripts/update_farps.py\` from base commit \`${base_sha}\`.
- Only ADF/FM and \`display\` are touched on airports. Everything else there
  comes from a terrain dump via \`scripts/import_airports.py\`.

</details>
EOF
}

open_pull_request() {
    local base_sha="$1"
    local body
    body="$(build_pr_body "${base_sha}")"

    if [[ ${DRY_RUN} -eq 1 ]]; then
        log_info "[DRY RUN] Would open a pull request from ${BRANCH} with body:"
        log_info "${body}"
        return 0
    fi

    # --label fails the whole command if the label does not exist, so create it
    # first and ignore the "already exists" case.
    gh label create "${PR_LABEL}" --color "1D76DB" \
        --description "Automated FARP data updates" >/dev/null 2>&1 || true

    gh pr create \
        --head "${BRANCH}" \
        --title "Update FARP data from live map ($(date -u '+%Y-%m-%d'))" \
        --body "${body}" \
        --label "${PR_LABEL}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    parse_arguments "$@"

    require_command git
    require_command python3
    [[ ${DRY_RUN} -eq 0 ]] && require_command gh

    cd "${REPO_ROOT}"

    for data_file in "${DATA_FILES[@]}"; do
        if [[ ! -f "${data_file}" ]]; then
            log_error "Missing ${data_file} in ${REPO_ROOT}"
            exit 1
        fi
    done
    if [[ ! -f "${MAP_DATA}" ]]; then
        log_error "Map data not found: ${MAP_DATA}. Run ${SCRIPT_DIR}/fetch-map-data.sh first."
        exit 1
    fi
    if ! git diff --quiet -- "${DATA_FILES[@]}"; then
        log_error "The data files have uncommitted changes; refusing to run"
        exit 1
    fi

    STARTING_BRANCH="$(git branch --show-current)"

    local base_sha
    base_sha="$(git rev-parse --short HEAD)"

    REPORT_FILE="$(mktemp)"
    AIRPORT_REPORT_FILE="$(mktemp)"
    COMMIT_MESSAGE_FILE="$(mktemp)"

    # Reading PR state is harmless, so do it under --dry-run too when gh is
    # available; only the mutations are suppressed.
    local existing_pr
    existing_pr=""
    if command -v gh >/dev/null 2>&1; then
        existing_pr="$(open_pr_number)"
    fi

    # Manual fields typed into the open pull request - an elevation, a corrected
    # shortname - live only on that branch. Seed the generator from it so that
    # replacing the pull request carries them forward instead of discarding them.
    if [[ -n "${existing_pr}" ]]; then
        fetch_pr_data || true
    fi

    local common_args=(--map-data "${MAP_DATA}")
    [[ ${QUIET} -eq 1 ]] && common_args+=(--quiet)
    [[ ${VERBOSE} -eq 1 ]] && common_args+=(--verbose)

    local farp_args=("${SCRIPT_DIR}/update_farps.py"
                     --farps "${FARPS_FILE}"
                     --report "${REPORT_FILE}"
                     --commit-message "${COMMIT_MESSAGE_FILE}"
                     "${common_args[@]}")
    local airport_args=("${SCRIPT_DIR}/update_airports.py"
                        --airports "${AIRPORTS_FILE}"
                        --report "${AIRPORT_REPORT_FILE}"
                        "${common_args[@]}")
    if [[ -n "${PR_FARPS_FILE}" ]]; then
        farp_args+=(--carry-from "${PR_FARPS_FILE}")
    fi
    if [[ -n "${PR_AIRPORTS_FILE}" ]]; then
        airport_args+=(--carry-from "${PR_AIRPORTS_FILE}")
    fi

    # Deliberately not wrapped in run_command: even under --dry-run we want the
    # generated file so the diff can be inspected. The working tree is restored
    # before the script exits.
    if ! python3 "${farp_args[@]}" || ! python3 "${airport_args[@]}"; then
        log_error "Data generation failed; leaving the data files untouched"
        git checkout -- "${DATA_FILES[@]}"
        exit 1
    fi

    if git diff --quiet -- "${DATA_FILES[@]}"; then
        log_info "FARP data matches the base branch; nothing to do"
        if [[ -n "${existing_pr}" ]]; then
            close_pr "${existing_pr}" \
                "Closing automatically: the base branch now matches the live map feed."
        fi
        return 0
    fi

    log_info "Data changed:"
    [[ ${QUIET} -eq 0 ]] && cat "${REPORT_FILE}" "${AIRPORT_REPORT_FILE}"

    if [[ -n "${existing_pr}" ]]; then
        local matches=1
        if [[ -n "${PR_FARPS_FILE}" && -n "${PR_AIRPORTS_FILE}" ]] \
           && cmp -s "${PR_FARPS_FILE}" "${FARPS_FILE}" \
           && cmp -s "${PR_AIRPORTS_FILE}" "${AIRPORTS_FILE}"; then
            matches=0
        fi

        if [[ ${matches} -eq 0 ]] && ! pr_has_conflicts "${existing_pr}"; then
            log_info "Open pull request #${existing_pr} already carries this data; nothing to do"
            git checkout -- "${DATA_FILES[@]}"
            return 0
        fi

        local close_reason
        if [[ ${matches} -eq 0 ]]; then
            close_reason="Closing automatically: this pull request conflicts with the base branch. A rebased replacement will be opened shortly."
        else
            close_reason="Closing automatically: the live map feed has moved on from this pull request. A replacement will be opened shortly."
        fi
        close_pr "${existing_pr}" "${close_reason}"
    fi

    if [[ ${DRY_RUN} -eq 1 ]]; then
        log_info "[DRY RUN] Proposed diff:"
        git --no-pager diff -- "${DATA_FILES[@]}" || true
        open_pull_request "${base_sha}"
        git checkout -- "${DATA_FILES[@]}"
        return 0
    fi

    # -B resets the branch to the current base commit while keeping the
    # regenerated file in the working tree, so each PR is a single clean commit.
    git switch -C "${BRANCH}"
    git add "${DATA_FILES[@]}"
    git commit --file "${COMMIT_MESSAGE_FILE}"
    git push --force --set-upstream origin "${BRANCH}"

    open_pull_request "${base_sha}"
}

main "$@"
