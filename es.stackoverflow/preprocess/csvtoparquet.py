"""Convert the prepared Stack Exchange CSV files to typed Parquet files.

The XML-to-CSV step intentionally discovers the columns present in the dump.
This second step applies the explicit, teaching-friendly schemas below.  It
uses Polars' lazy CSV scan and streaming batches to sort each table by ``Id``;
PyArrow's ParquetWriter writes the final file so the exact Arrow schema and
field metadata are preserved.  The independent tables are converted in
parallel using worker processes.

Example:

    python3 csvtoparquet.py --input-dir data --output-dir data

The output directory receives one Parquet file per table. Packaging is
deliberately left to the CI workflow.
"""

from __future__ import annotations

import argparse
import csv
import multiprocessing as mp
import os
from collections.abc import Iterator, Mapping
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias, cast

import pyarrow as pa
import pyarrow.parquet as pq

if TYPE_CHECKING:
    import polars as pl

# ---------------------------------------------------------------------------
# Explicit schemas
# ---------------------------------------------------------------------------
#
# These are the output schemas.  The ``nullable`` flag describes the value in
# the Parquet file, not merely the Arrow type:
#
#   * counters and metrics are filled with 0 before writing and are NOT NULL;
#   * optional foreign keys remain NULL when they are absent from the dump;
#   * optional dates and strings remain NULL when they are absent.
#
# The metadata is deliberately small and human-readable.  It is embedded in
# the Arrow schema stored in the Parquet file.  It
# is useful when generating database DDL, but it is not itself a SQL default
# or a foreign-key constraint.
#
# ``FavoriteCount`` is deliberately absent from ``POSTS_SCHEMA``.  It is a
# legacy field retained in historical documentation, but current public dumps
# no longer export it after Favorites/Bookmarks were replaced by private
# Saves.  Its absence means "not available", not zero.

POSTS_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field(
            "Id",
            pa.int64(),
            nullable=False,
            metadata={b"role": b"primary_key"},
        ),
        pa.field(
            "AcceptedAnswerId",
            pa.int64(),
            nullable=True,
            metadata={
                b"role": b"foreign_key",
                b"references": b"Posts.Id",
                b"missing": b"null",
            },
        ),
        pa.field(
            "AnswerCount",
            pa.int32(),
            nullable=False,
            metadata={b"default": b"0", b"missing": b"zero"},
        ),
        pa.field("Body", pa.string(), nullable=True),
        pa.field("ClosedDate", pa.timestamp("ms"), nullable=True),
        pa.field(
            "CommentCount",
            pa.int32(),
            nullable=False,
            metadata={b"default": b"0", b"missing": b"zero"},
        ),
        pa.field("CommunityOwnedDate", pa.timestamp("ms"), nullable=True),
        pa.field("ContentLicense", pa.string(), nullable=True),
        pa.field("CreationDate", pa.timestamp("ms"), nullable=False),
        pa.field("LastActivityDate", pa.timestamp("ms"), nullable=False),
        pa.field("LastEditDate", pa.timestamp("ms"), nullable=True),
        pa.field("LastEditorDisplayName", pa.string(), nullable=True),
        pa.field(
            "LastEditorUserId",
            pa.int64(),
            nullable=True,
            metadata={
                b"role": b"foreign_key",
                b"references": b"Users.Id",
                b"missing": b"null",
            },
        ),
        pa.field("OwnerDisplayName", pa.string(), nullable=True),
        pa.field(
            "OwnerUserId",
            pa.int64(),
            nullable=True,
            metadata={
                b"role": b"foreign_key",
                b"references": b"Users.Id",
                b"missing": b"null",
            },
        ),
        pa.field(
            "ParentId",
            pa.int64(),
            nullable=True,
            metadata={
                b"role": b"foreign_key",
                b"references": b"Posts.Id",
                b"missing": b"null",
            },
        ),
        pa.field("PostTypeId", pa.int8(), nullable=False),
        pa.field(
            "Score",
            pa.int32(),
            nullable=False,
            metadata={b"default": b"0", b"missing": b"zero"},
        ),
        pa.field("Tags", pa.string(), nullable=True),
        pa.field("Title", pa.string(), nullable=True),
        pa.field(
            "ViewCount",
            pa.int64(),
            nullable=False,
            metadata={b"default": b"0", b"missing": b"zero"},
        ),
    ]
)

VOTES_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("Id", pa.int64(), nullable=False,
                 metadata={b"role": b"primary_key"}),
        pa.field(
            "BountyAmount",
            pa.int32(),
            nullable=False,
            metadata={b"default": b"0", b"missing": b"zero"},
        ),
        pa.field("CreationDate", pa.timestamp("ms"), nullable=False),
        pa.field(
            "PostId",
            pa.int64(),
            nullable=False,
            metadata={b"role": b"foreign_key", b"references": b"Posts.Id"},
        ),
        pa.field(
            "UserId",
            pa.int64(),
            nullable=True,
            metadata={
                b"role": b"foreign_key",
                b"references": b"Users.Id",
                b"missing": b"null",
            },
        ),
        pa.field("VoteTypeId", pa.int8(), nullable=False),
    ]
)

USERS_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("Id", pa.int64(), nullable=False,
                 metadata={b"role": b"primary_key"}),
        pa.field("AboutMe", pa.string(), nullable=True),
        pa.field("AccountId", pa.int64(), nullable=True),
        pa.field("CreationDate", pa.timestamp("ms"), nullable=False),
        pa.field("DisplayName", pa.string(), nullable=False),
        pa.field(
            "DownVotes",
            pa.int32(),
            nullable=False,
            metadata={b"default": b"0", b"missing": b"zero"},
        ),
        pa.field("LastAccessDate", pa.timestamp("ms"), nullable=True),
        pa.field("Location", pa.string(), nullable=True),
        pa.field(
            "Reputation",
            pa.int32(),
            nullable=False,
            metadata={b"default": b"0", b"missing": b"zero"},
        ),
        pa.field(
            "UpVotes",
            pa.int32(),
            nullable=False,
            metadata={b"default": b"0", b"missing": b"zero"},
        ),
        pa.field(
            "Views",
            pa.int32(),
            nullable=False,
            metadata={b"default": b"0", b"missing": b"zero"},
        ),
        pa.field("WebsiteUrl", pa.string(), nullable=True),
    ]
)

TAGS_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("Id", pa.int64(), nullable=False,
                 metadata={b"role": b"primary_key"}),
        pa.field(
            "Count",
            pa.int32(),
            nullable=False,
            metadata={b"default": b"0", b"missing": b"zero"},
        ),
        pa.field(
            "ExcerptPostId",
            pa.int64(),
            nullable=True,
            metadata={
                b"role": b"foreign_key",
                b"references": b"Posts.Id",
                b"missing": b"null",
            },
        ),
        pa.field("TagName", pa.string(), nullable=False),
        pa.field(
            "WikiPostId",
            pa.int64(),
            nullable=True,
            metadata={
                b"role": b"foreign_key",
                b"references": b"Posts.Id",
                b"missing": b"null",
            },
        ),
    ]
)

COMMENTS_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("Id", pa.int64(), nullable=False,
                 metadata={b"role": b"primary_key"}),
        pa.field("ContentLicense", pa.string(), nullable=True),
        pa.field("CreationDate", pa.timestamp("ms"), nullable=False),
        pa.field(
            "PostId",
            pa.int64(),
            nullable=False,
            metadata={b"role": b"foreign_key", b"references": b"Posts.Id"},
        ),
        pa.field(
            "Score",
            pa.int32(),
            nullable=False,
            metadata={b"default": b"0", b"missing": b"zero"},
        ),
        pa.field("Text", pa.string(), nullable=True),
        pa.field("UserDisplayName", pa.string(), nullable=True),
        pa.field(
            "UserId",
            pa.int64(),
            nullable=True,
            metadata={
                b"role": b"foreign_key",
                b"references": b"Users.Id",
                b"missing": b"null",
            },
        ),
    ]
)


@dataclass(frozen=True)
class TableSpec:
    schema: pa.Schema
    zero_defaults: Mapping[str, int]


TableTask: TypeAlias = tuple[str, Path, Path]


# The names here must match the CSV files produced by preprocess.sh.
TABLES: dict[str, TableSpec] = {
    "Posts": TableSpec(
        POSTS_SCHEMA,
        {
            "AnswerCount": 0,
            "CommentCount": 0,
            "Score": 0,
            "ViewCount": 0,
        },
    ),
    "Votes": TableSpec(VOTES_SCHEMA, {"BountyAmount": 0}),
    "Users": TableSpec(
        USERS_SCHEMA,
        {"DownVotes": 0, "Reputation": 0, "UpVotes": 0, "Views": 0},
    ),
    "Tags": TableSpec(TAGS_SCHEMA, {"Count": 0}),
    "Comments": TableSpec(COMMENTS_SCHEMA, {"Score": 0}),
}


# Polars uses a lazy CSV scan and streaming batches.  Explicit column types
# avoid inference errors and let the optimizer keep the scan lazy until the
# sorted results are consumed.  Sorting is a global operation, so it can still
# require a substantial amount of memory even though results are batched.
CSV_BATCH_SIZE: int = 100_000
PARQUET_ROW_GROUP_SIZE: int = 100_000
PARQUET_COMPRESSION: str = "brotli"
PARQUET_COMPRESSION_LEVEL: int = 11
SORT_COLUMN: str = "Id"


def _read_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        try:
            rows: Iterator[list[str]] = csv.reader(source)
            return next(rows)
        except StopIteration as exc:
            raise ValueError(f"CSV file is empty: {path}") from exc


def _validate_header(
    path: Path,
    schema: pa.Schema,
    zero_defaults: Mapping[str, int],
) -> list[str]:
    actual: list[str] = _read_header(path)
    if len(actual) != len(set(actual)):
        raise ValueError(f"CSV header contains duplicate columns: {path}")

    expected: set[str] = set(schema.names)
    actual_set: set[str] = set(actual)
    missing: list[str] = [name for name in schema.names if name not in actual_set]
    extra: list[str] = [name for name in actual if name not in expected]
    # An optional attribute may be absent from every XML row and therefore
    # from the discovered CSV header.  Defaulted numeric columns are also
    # allowed to be absent: they become an all-null input column and are then
    # filled with zero below.  Other missing non-nullable fields indicate a
    # broken/incompatible input and must fail loudly.
    nullable_fields: set[str] = {
        field.name for field in schema if field.nullable
    }
    allowed_missing: set[str] = nullable_fields | set(zero_defaults)
    missing_required: list[str] = [
        name for name in missing if name not in allowed_missing
    ]
    if missing_required or extra:
        details: list[str] = []
        if missing_required:
            details.append(
                f"missing required columns: {', '.join(missing_required)}"
            )
        if extra:
            details.append(f"unexpected columns: {', '.join(extra)}")
        raise ValueError(f"Unexpected schema in {path}: {'; '.join(details)}")
    return actual


def _polars_dtype(data_type: pa.DataType) -> pl.DataType:
    """Map one supported Arrow type to its equivalent Polars dtype."""

    import polars as pl

    if pa.types.is_int8(data_type):
        return pl.Int8
    if pa.types.is_int32(data_type):
        return pl.Int32
    if pa.types.is_int64(data_type):
        return pl.Int64
    if pa.types.is_string(data_type):
        return pl.String
    if pa.types.is_timestamp(data_type):
        timestamp_type: pa.TimestampType = cast(pa.TimestampType, data_type)
        return pl.Datetime(time_unit=timestamp_type.unit)
    raise TypeError(f"Unsupported Arrow type in explicit schema: {data_type}")


def _polars_overrides(
    schema: pa.Schema,
    header: list[str],
) -> dict[str, pl.DataType]:
    """Return explicit Polars dtypes for the columns present in the CSV."""

    header_set: set[str] = set(header)
    return {
        field.name: _polars_dtype(field.type)
        for field in schema
        if field.name in header_set
    }


def _prepare_lazy_frame(
    input_path: Path,
    schema: pa.Schema,
    zero_defaults: Mapping[str, int],
    header: list[str],
) -> pl.LazyFrame:
    """Build the typed, normalized and ordered lazy CSV-to-Parquet plan."""

    import polars as pl

    schema_overrides: dict[str, pl.DataType] = _polars_overrides(
        schema, header
    )
    lazy_frame: pl.LazyFrame = pl.scan_csv(
        input_path,
        has_header=True,
        separator=",",
        quote_char='"',
        encoding="utf8",
        schema_overrides=schema_overrides,
        null_values="",
        empty_string_is_null=True,
        infer_schema=False,
        try_parse_dates=False,
        ignore_errors=False,
        low_memory=True,
    )

    # A field may be absent from every XML row and therefore absent from the
    # discovered CSV header.  Recreate it lazily with either its documented
    # zero default or NULL.  Required fields were rejected by _validate_header.
    missing_columns: list[pl.Expr] = [
        pl.lit(
            zero_defaults.get(name, None)
        )
        .cast(_polars_dtype(field.type))
        .alias(name)
        for field in schema
        for name in [field.name]
        if name not in header
    ]
    if missing_columns:
        lazy_frame = lazy_frame.with_columns(missing_columns)

    zero_fill_columns: list[pl.Expr] = [
        pl.col(name)
        .fill_null(default)
        .cast(_polars_dtype(schema.field(name).type))
        .alias(name)
        for name, default in zero_defaults.items()
        if name in header
    ]
    if zero_fill_columns:
        lazy_frame = lazy_frame.with_columns(zero_fill_columns)

    # Select also fixes the output column order, independently of the order
    # discovered by schemaextract.py.
    return lazy_frame.select(schema.names).sort(SORT_COLUMN)


def convert_table(name: str, input_dir: Path, output_dir: Path) -> Path:
    """Convert one CSV table to sorted, explicitly typed Parquet."""

    spec: TableSpec = TABLES[name]
    input_path: Path = input_dir / f"{name}.csv"
    output_path: Path = output_dir / f"{name}.parquet"

    if not input_path.is_file():
        raise FileNotFoundError(f"Input CSV not found: {input_path}")
    if not pa.Codec.is_available(PARQUET_COMPRESSION):
        raise RuntimeError(
            f"PyArrow was built without {PARQUET_COMPRESSION} compression support"
        )
    header: list[str] = _validate_header(
        input_path, spec.schema, spec.zero_defaults
    )
    lazy_frame: pl.LazyFrame = _prepare_lazy_frame(
        input_path, spec.schema, spec.zero_defaults, header
    )
    print(f"Converting {input_path} -> {output_path}", flush=True)
    # Polars' native sink is excellent for a fully-Polars schema, but its
    # String columns currently export as Arrow large_string.  The teaching
    # schemas intentionally use the more portable pa.string(), and also carry
    # field metadata and non-nullability.  Consume Polars' sorted streaming
    # batches and use PyArrow as the exact-schema writer at this boundary.
    batches: Iterator[pl.DataFrame] = lazy_frame.collect_batches(
        chunk_size=CSV_BATCH_SIZE,
        maintain_order=True,
        engine="streaming",
    )
    rows: int = 0
    writer: pq.ParquetWriter
    with pq.ParquetWriter(
        output_path,
        spec.schema,
        version="2.6",
        compression=PARQUET_COMPRESSION,
        compression_level=PARQUET_COMPRESSION_LEVEL,
        use_dictionary=True,
        write_statistics=True,
        data_page_version="1.0",
        store_schema=True,
    ) as writer:
        for frame in batches:
            table: pa.Table = frame.to_arrow().cast(spec.schema, safe=True)
            table.validate(full=True)
            writer.write_table(table, row_group_size=PARQUET_ROW_GROUP_SIZE)
            rows += table.num_rows

    print(
        f"  {rows:,} rows; wrote sorted Parquet by {SORT_COLUMN}",
        flush=True,
    )
    return output_path


def _convert_table_worker(task: TableTask) -> Path:
    """Run one table conversion in a worker process."""

    # The parent intentionally starts up to four processes per CPU because
    # this workload has substantial I/O.  A single Polars thread per process
    # avoids turning that into four nested CPU thread pools per process.
    os.environ.setdefault("POLARS_MAX_THREADS", "1")

    name: str
    input_dir: Path
    output_dir: Path
    name, input_dir, output_dir = task
    return convert_table(name, input_dir, output_dir)


def _default_worker_count() -> int:
    """Use four workers per CPU, bounded by the five independent tables."""

    available_cpus: int = os.cpu_count() or 1
    return min(len(TABLES), available_cpus * 4)


def convert_tables(
    input_dir: Path,
    output_dir: Path,
    workers: int,
) -> list[Path]:
    """Convert all tables, using one process per active table at most."""

    tasks: list[TableTask] = [
        (name, input_dir, output_dir)
        for name in TABLES
    ]
    if workers < 1:
        raise ValueError("workers must be at least 1")
    worker_count: int = min(workers, len(tasks))
    if worker_count == 1:
        return [_convert_table_worker(task) for task in tasks]

    # ``spawn`` is explicit so this remains safe on platforms where forking
    # after importing PyArrow (which has native thread pools) is problematic.
    with ProcessPoolExecutor(
        max_workers=worker_count,
        mp_context=mp.get_context("spawn"),
    ) as executor:
        return list(executor.map(_convert_table_worker, tasks, chunksize=1))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert prepared Stack Exchange CSV files to Parquet."
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("data"),
        help="directory containing Posts.csv, Votes.csv, ... (default: data)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="output directory (default: same as --input-dir)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help=(
            "number of table conversion processes "
            f"(default: {_default_worker_count()})"
        ),
    )
    args: argparse.Namespace = parser.parse_args(argv)

    input_dir: Path = cast(Path, args.input_dir)
    output_dir_arg: Path | None = cast(Path | None, args.output_dir)
    output_dir: Path = output_dir_arg or input_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    workers_arg: int | None = cast(int | None, args.workers)
    workers: int = (
        workers_arg
        if workers_arg is not None
        else _default_worker_count()
    )
    if workers < 1:
        parser.error("--workers must be at least 1")
    workers = min(workers, len(TABLES))
    print(f"Converting {len(TABLES)} tables with {workers} worker(s)", flush=True)
    convert_tables(input_dir, output_dir, workers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
