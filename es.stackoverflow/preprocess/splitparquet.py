"""Prepare the Parquet files that are committed to the repository.

The data release is the source of truth, but its assets are served without
CORS headers, so a web page cannot fetch them; files committed to the
repository are served by raw.githubusercontent.com, which does send them.
GitHub refuses files over 100 MB and ``Posts.parquet`` is larger than that,
so this script splits it into ``Posts1.parquet``, ``Posts2.parquet``, ...
Tables that already fit are copied byte for byte.

A split table is cut only *between* row groups, so every piece holds whole row
groups exactly as they are in the release file, in ``Id`` order: reading the
pieces one after the other gives back the original table.  The pieces are
written with the options of ``csvtoparquet.writer_options`` (schema, Brotli,
statistics, Bloom filters, ...) and checked against the input before the
script succeeds.  Never split by ``Id`` range: that does not balance the sizes.

Regenerating is skipped when the *origin* has not changed, so a run that would
only rewrite the same data adds nothing to the git history.  The origin is the
Stack Exchange dump the release was built from: ``source.json`` in the
repository (dump URL and sha256, written by the workflow that builds the
release), which ``--source-json`` reads.
``manifest.json`` in the output directory records that origin, the split and
the digest of every output file; the outputs are rebuilt only if the origin,
the split or a file on disk differs from it, or with ``--force``.  Without a
``source.json`` the origin is unknown and the outputs are always rebuilt.

Example:

    python3 splitparquet.py --input-dir data --output-dir ../parquet
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import multiprocessing as mp
import re
import shutil
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq

from csvtoparquet import SORT_COLUMN, TABLES, writer_options

# GitHub refuses pushes with files over 100 MB; stay clearly below it.
MAX_FILE_BYTES: int = 95 * 1024 * 1024
DEFAULT_SPLITS: dict[str, int] = {"Posts": 3}
DEFAULT_SOURCE_RELEASE: str = "es.stackoverflow.data-26-27"
MANIFEST_NAME: str = "manifest.json"


def _row_group_sizes(metadata: pq.FileMetaData) -> list[int]:
    """Compressed size of every row group."""

    return [
        sum(
            metadata.row_group(index).column(column).total_compressed_size
            for column in range(metadata.num_columns)
        )
        for index in range(metadata.num_row_groups)
    ]


def plan_split(sizes: list[int], parts: int) -> list[range]:
    """Cut the row groups into ``parts`` runs, minimizing the largest run."""

    if not 1 <= parts <= len(sizes):
        raise ValueError(
            f"cannot split {len(sizes)} row groups into {parts} parts"
        )
    best: list[range] | None = None
    best_largest: int = 0
    for cuts in itertools.combinations(range(1, len(sizes)), parts - 1):
        bounds: list[int] = [0, *cuts, len(sizes)]
        runs: list[range] = [
            range(bounds[i], bounds[i + 1]) for i in range(parts)
        ]
        largest: int = max(sum(sizes[i] for i in run) for run in runs)
        if best is None or largest < best_largest:
            best, best_largest = runs, largest
    assert best is not None
    return best


def _write_part(task: tuple[str, Path, Path, range]) -> Path:
    """Write one piece: the given row groups of ``input_path``."""

    name, input_path, output_path, row_groups = task
    source = pq.ParquetFile(input_path)
    temporary: Path = output_path.with_name(output_path.name + ".part")
    print(f"Writing {output_path.name}: row groups {row_groups.start}"
          f"-{row_groups.stop - 1}", flush=True)
    with pq.ParquetWriter(
        temporary, TABLES[name].schema, **writer_options(TABLES[name])
    ) as writer:
        for index in row_groups:
            table: pa.Table = source.read_row_group(index)
            writer.write_table(table, row_group_size=table.num_rows)
    temporary.replace(output_path)
    return output_path


def _verify_split(
    name: str, input_path: Path, outputs: list[tuple[Path, range]]
) -> None:
    """Fail unless the pieces are the input table, cut between row groups."""

    source = pq.ParquetFile(input_path)
    expected_schema: pa.Schema = TABLES[name].schema
    previous_last_id: int | None = None
    total_rows: int = 0
    for path, row_groups in outputs:
        piece = pq.ParquetFile(path)
        if not piece.schema_arrow.equals(expected_schema, check_metadata=True):
            raise ValueError(f"{path.name}: schema differs from the table's")
        if piece.metadata.num_row_groups != len(row_groups):
            raise ValueError(f"{path.name}: unexpected number of row groups")
        for offset, index in enumerate(row_groups):
            if not piece.read_row_group(offset).equals(
                source.read_row_group(index)
            ):
                raise ValueError(f"{path.name}: row group {offset} differs")
        statistics = piece.metadata.row_group(0).column(0).statistics
        if previous_last_id is not None and statistics.min <= previous_last_id:
            raise ValueError(f"{path.name}: {SORT_COLUMN} order not preserved")
        last = piece.metadata.row_group(piece.metadata.num_row_groups - 1)
        previous_last_id = last.column(0).statistics.max
        total_rows += piece.metadata.num_rows
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(
                f"{path.name} is {path.stat().st_size / 1e6:,.0f} MB, over "
                f"the {MAX_FILE_BYTES / 1e6:,.0f} MB limit for a file in the "
                "repository: use more parts"
            )
    if total_rows != source.metadata.num_rows:
        raise ValueError(f"{name}: {total_rows} rows, expected "
                         f"{source.metadata.num_rows}")


def _file_entry(path: Path) -> dict[str, int | str]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return {"bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def read_source(path: Path | None) -> dict[str, Any] | None:
    """The origin recorded by the release, or None if it does not say."""

    if path is None or not path.is_file():
        return None
    return cast(dict[str, Any], json.loads(path.read_text()))


def _is_up_to_date(
    output_dir: Path, source: dict[str, Any] | None, split: dict[str, int]
) -> bool:
    """True if ``output_dir`` already holds the outputs for this origin."""

    if source is None:
        return False
    try:
        manifest = json.loads((output_dir / MANIFEST_NAME).read_text())
    except (OSError, ValueError):
        return False
    if manifest.get("source") != source or manifest.get("split") != split:
        return False
    files: dict[str, dict[str, int | str]] = manifest.get("files", {})
    on_disk: set[str] = {path.name for path in output_dir.glob("*.parquet")}
    return on_disk == set(files) and all(
        _file_entry(output_dir / name) == entry for name, entry in files.items()
    )


def _write_manifest(
    output_dir: Path,
    source_release: str,
    source: dict[str, Any] | None,
    split: dict[str, int],
) -> None:
    manifest = {
        "source_release": source_release,
        "source": source,
        "split": split,
        "files": {
            path.name: _file_entry(path)
            for path in sorted(output_dir.glob("*.parquet"))
        },
    }
    (output_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )


def _remove_stale(output_dir: Path, name: str) -> None:
    """Remove earlier outputs of ``name`` (``Posts.parquet``, ``Posts2.parquet``)."""

    stale = re.compile(rf"{re.escape(name)}\d*\.parquet(\.part)?")
    for path in output_dir.iterdir():
        if stale.fullmatch(path.name):
            path.unlink()


def prepare(
    input_dir: Path,
    output_dir: Path,
    splits: dict[str, int],
    workers: int,
    source_release: str,
    source: dict[str, Any] | None,
    force: bool,
) -> bool:
    """Fill ``output_dir``; return False if it was already up to date."""

    output_dir.mkdir(parents=True, exist_ok=True)
    for name in TABLES:
        if not (input_dir / f"{name}.parquet").is_file():
            raise FileNotFoundError(f"{input_dir / f'{name}.parquet'}")

    split: dict[str, int] = {name: splits.get(name, 1) for name in TABLES}
    if source is None:
        print("warning: no source.json, the origin is unknown", flush=True)
    if not force and _is_up_to_date(output_dir, source, split):
        print(f"{output_dir}: built from the same source dump as the release "
              "(use --force to rebuild); nothing to do")
        return False

    jobs: list[tuple[str, Path, Path, range]] = []
    plans: dict[str, list[tuple[Path, range]]] = {}
    for name in TABLES:
        input_path: Path = input_dir / f"{name}.parquet"
        parts: int = splits.get(name, 1)
        _remove_stale(output_dir, name)
        if parts == 1:
            if input_path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError(
                    f"{input_path.name} is over the size limit for a file in "
                    f"the repository: add --split {name}=N"
                )
            shutil.copyfile(input_path, output_dir / input_path.name)
            print(f"Copied {input_path.name}", flush=True)
            continue
        sizes = _row_group_sizes(pq.ParquetFile(input_path).metadata)
        plans[name] = []
        for number, run in enumerate(plan_split(sizes, parts), start=1):
            output_path: Path = output_dir / f"{name}{number}.parquet"
            plans[name].append((output_path, run))
            jobs.append((name, input_path, output_path, run))

    if jobs:
        with ProcessPoolExecutor(
            max_workers=max(1, min(workers, len(jobs))),
            mp_context=mp.get_context("spawn"),
        ) as executor:
            list(executor.map(_write_part, jobs, chunksize=1))

    for name, outputs in plans.items():
        _verify_split(name, input_dir / f"{name}.parquet", outputs)
        for path, _ in outputs:
            print(f"  {path.name}: {path.stat().st_size / 1e6:,.1f} MB")
        print(f"{name}: {len(outputs)} pieces verified against the input")
    _write_manifest(output_dir, source_release, source, split)
    return True


def _parse_split(value: str) -> tuple[str, int]:
    name, _, parts = value.partition("=")
    if name not in TABLES or not parts.isdigit() or int(parts) < 1:
        raise argparse.ArgumentTypeError(
            f"expected TABLE=N with TABLE one of {', '.join(TABLES)}"
        )
    return name, int(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare the Parquet files committed to the repository."
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("data"),
        help="directory with the release's Posts.parquet, ... (default: data)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("../parquet"),
        help="directory to fill (default: ../parquet)",
    )
    parser.add_argument(
        "--split",
        type=_parse_split,
        action="append",
        metavar="TABLE=N",
        help=f"split TABLE into N pieces (default: "
        f"{', '.join(f'{k}={v}' for k, v in DEFAULT_SPLITS.items())})",
    )
    parser.add_argument(
        "--source-json",
        type=Path,
        default=None,
        help="source.json: the dump the release was built from "
        "(default: none, the origin is unknown and everything is rebuilt)",
    )
    parser.add_argument(
        "--source-release",
        default=DEFAULT_SOURCE_RELEASE,
        help="release tag the inputs come from, recorded in the manifest "
        f"(default: {DEFAULT_SOURCE_RELEASE})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="rebuild even if the source data is unchanged",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="processes writing pieces at the same time (default: 4)",
    )
    args: argparse.Namespace = parser.parse_args(argv)
    splits: dict[str, int] = (
        dict(cast(list[tuple[str, int]], args.split))
        if args.split
        else dict(DEFAULT_SPLITS)
    )
    prepare(
        cast(Path, args.input_dir),
        cast(Path, args.output_dir),
        splits,
        cast(int, args.workers),
        cast(str, args.source_release),
        read_source(cast(Path | None, args.source_json)),
        cast(bool, args.force),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
