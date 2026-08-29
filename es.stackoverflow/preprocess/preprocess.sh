#!/bin/sh

# Convert XML -> CSV in parallel.
# Use GNU parallel with --link to pair input and output filenames.
# If GNU parallel isn't available, fall back to xargs.

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEFAULT_DATA_DIR="${SCRIPT_DIR%/}/data"
DATA_DIR=${DATA_DIR:-$DEFAULT_DATA_DIR}
URL_FILE=${URL_FILE:-"${SCRIPT_DIR%/}/URL"}
ARCHIVE_PATH=${ARCHIVE_PATH:-"$DATA_DIR/es.stackoverflow.com.7z"}

INPUTS="$DATA_DIR/Posts.xml $DATA_DIR/Votes.xml $DATA_DIR/Users.xml $DATA_DIR/Tags.xml $DATA_DIR/Comments.xml"
OUTPUTS="$DATA_DIR/Posts.csv $DATA_DIR/Votes.csv $DATA_DIR/Users.csv $DATA_DIR/Tags.csv $DATA_DIR/Comments.csv"

inputs_present() {
    for input in $INPUTS; do
        [ -f "$input" ] || return 1
    done
}

report_missing_inputs() {
    for input in $INPUTS; do
        if [ ! -f "$input" ]; then
            echo "Missing input file: $input" >&2
        fi
    done
    echo "Extract the dump XML files directly into $DATA_DIR, or set DATA_DIR." >&2
}

if ! inputs_present || [ "${FORCE_DOWNLOAD:-0}" = 1 ]; then
    if [ "${DOWNLOAD:-1}" != 1 ]; then
        report_missing_inputs
        exit 1
    fi

    mkdir -p "$DATA_DIR"

    if [ "${FORCE_DOWNLOAD:-0}" = 1 ] || [ ! -s "$ARCHIVE_PATH" ]; then
        if ! command -v curl >/dev/null 2>&1; then
            echo "curl is required to download the dump." >&2
            exit 1
        fi
        if [ -z "${DUMP_URL:-}" ]; then
            if [ ! -r "$URL_FILE" ]; then
                echo "URL file not found: $URL_FILE" >&2
                exit 1
            fi
            DUMP_URL=$(sed -n '1p' "$URL_FILE")
        fi
        if [ -z "$DUMP_URL" ]; then
            echo "The dump URL is empty: $URL_FILE" >&2
            exit 1
        fi

        echo "Downloading dump from $DUMP_URL" >&2
        curl --fail --location --retry 3 --retry-delay 5 \
            --output "$ARCHIVE_PATH.part" "$DUMP_URL"
        mv -f "$ARCHIVE_PATH.part" "$ARCHIVE_PATH"
    fi

    if ! command -v 7z >/dev/null 2>&1; then
        echo "7z is required to extract the dump archive." >&2
        exit 1
    fi

    echo "Extracting $ARCHIVE_PATH into $DATA_DIR" >&2
    7z x -y "$ARCHIVE_PATH" "-o$DATA_DIR"
fi

if ! inputs_present; then
    report_missing_inputs
    exit 1
fi

if command -v nproc >/dev/null 2>&1; then
    JOBS=$(nproc)
else
    JOBS=1
fi

if command -v parallel >/dev/null 2>&1 && parallel --version 2>/dev/null | grep -q 'GNU parallel'; then
    # --link pairs the Nth arg from each ::: list
    parallel --halt soon,fail=1 --link python3 "$SCRIPT_DIR/xmltocsv.py" {1} {2} ::: $INPUTS ::: $OUTPUTS
else
    # Fallback: use xargs to run two-argument jobs in parallel
    xargs -n2 -P"$JOBS" python3 "$SCRIPT_DIR/xmltocsv.py" <<EOF
$DATA_DIR/Posts.xml $DATA_DIR/Posts.csv
$DATA_DIR/Votes.xml $DATA_DIR/Votes.csv
$DATA_DIR/Users.xml $DATA_DIR/Users.csv
$DATA_DIR/Tags.xml $DATA_DIR/Tags.csv
$DATA_DIR/Comments.xml $DATA_DIR/Comments.csv
EOF
fi
