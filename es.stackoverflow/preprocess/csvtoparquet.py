#!/usr/bin/env python3
"""Convert the prepared Stack Exchange CSV files to typed Parquet files.

The XML-to-CSV step intentionally discovers the columns present in the dump.
This second step applies the explicit, teaching-friendly schemas below.  It
reads CSV incrementally and writes Parquet incrementally, so it does not need
to hold a complete table in memory.

Example:

    python3 csvtoparquet.py --input-dir data --output-dir data

The output directory receives one Parquet file per table. Packaging is
deliberately left to the CI workflow.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.parquet as pq


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
# the Arrow schema stored in the Parquet file (store_schema=True below).  It
# is useful when generating database DDL, but it is not itself a SQL default
# or a foreign-key constraint.
#
# ``FavoriteCount`` is deliberately absent from ``POSTS_SCHEMA``.  It is a
# legacy field retained in historical documentation, but current public dumps
# no longer export it after Favorites/Bookmarks were replaced by private
# Saves.  Its absence means "not available", not zero.

POSTS_SCHEMA = pa.schema(
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

VOTES_SCHEMA = pa.schema(
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

USERS_SCHEMA = pa.schema(
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

TAGS_SCHEMA = pa.schema(
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

COMMENTS_SCHEMA = pa.schema(
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


# ``open_csv`` is the memory-efficient streaming reader.  Its incremental
# reader is single-threaded, but explicit column types avoid type inference
# errors and the process never needs to materialize the whole CSV.
CSV_BLOCK_SIZE = 32 * 1024 * 1024
PARQUET_ROW_GROUP_SIZE = 100_000
PARQUET_COMPRESSION = "brotli"
PARQUET_COMPRESSION_LEVEL = 11


def _all_nullable_schema(schema: pa.Schema) -> pa.Schema:
    """Return the input schema used while CSV nulls are still being handled."""

    return pa.schema(
        [
            pa.field(
                field.name,
                field.type,
                nullable=True,
                metadata=field.metadata,
            )
            for field in schema
        ],
        metadata=schema.metadata,
    )


def _read_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        try:
            return next(csv.reader(source))
        except StopIteration as exc:
            raise ValueError(f"CSV file is empty: {path}") from exc


def _validate_header(
    path: Path,
    schema: pa.Schema,
    zero_defaults: Mapping[str, int],
) -> None:
    actual = _read_header(path)
    if len(actual) != len(set(actual)):
        raise ValueError(f"CSV header contains duplicate columns: {path}")

    expected = set(schema.names)
    actual_set = set(actual)
    missing = [name for name in schema.names if name not in actual_set]
    extra = [name for name in actual if name not in expected]
    # An optional attribute may be absent from every XML row and therefore
    # from the discovered CSV header.  Defaulted numeric columns are also
    # allowed to be absent: they become an all-null input column and are then
    # filled with zero below.  Other missing non-nullable fields indicate a
    # broken/incompatible input and must fail loudly.
    nullable_fields = {
        field.name for field in schema if field.nullable
    }
    allowed_missing = nullable_fields | set(zero_defaults)
    missing_required = [name for name in missing if name not in allowed_missing]
    if missing_required or extra:
        details = []
        if missing_required:
            details.append(
                f"missing required columns: {', '.join(missing_required)}"
            )
        if extra:
            details.append(f"unexpected columns: {', '.join(extra)}")
        raise ValueError(f"Unexpected schema in {path}: {'; '.join(details)}")


def _normalise_batch(
    batch: pa.RecordBatch,
    schema: pa.Schema,
    zero_defaults: Mapping[str, int],
) -> pa.Table:
    table = pa.Table.from_batches([batch])

    for name, default in zero_defaults.items():
        field = schema.field(name)
        array = pc.fill_null(table[name], pa.scalar(default, type=field.type))
        table = table.set_column(table.schema.get_field_index(name), name, array)

    # This also verifies that required fields really contain no nulls and that
    # values fit in the explicitly chosen integer/timestamp types.
    result = table.select(schema.names).cast(schema, safe=True)
    result.validate(full=True)
    return result


def convert_table(name: str, input_dir: Path, output_dir: Path) -> Path:
    spec = TABLES[name]
    input_path = input_dir / f"{name}.csv"
    output_path = output_dir / f"{name}.parquet"

    if not input_path.is_file():
        raise FileNotFoundError(f"Input CSV not found: {input_path}")
    _validate_header(input_path, spec.schema, spec.zero_defaults)

    if not pa.Codec.is_available(PARQUET_COMPRESSION):
        raise RuntimeError(
            f"PyArrow was built without {PARQUET_COMPRESSION} compression support"
        )

    read_options = pacsv.ReadOptions(
        use_threads=True,
        block_size=CSV_BLOCK_SIZE,
        encoding="utf-8",
    )
    parse_options = pacsv.ParseOptions(
        delimiter=",",
        quote_char='"',
        double_quote=True,
        newlines_in_values=False,
    )
    convert_options = pacsv.ConvertOptions(
        column_types=_all_nullable_schema(spec.schema),
        include_columns=spec.schema.names,
        include_missing_columns=True,
        null_values=[""],
        strings_can_be_null=True,
        quoted_strings_can_be_null=True,
        check_utf8=True,
    )

    reader = pacsv.open_csv(
        input_path,
        read_options=read_options,
        parse_options=parse_options,
        convert_options=convert_options,
    )

    rows = 0
    print(f"Converting {input_path} -> {output_path}", flush=True)
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
        for batch in reader:
            table = _normalise_batch(batch, spec.schema, spec.zero_defaults)
            writer.write_table(table, row_group_size=PARQUET_ROW_GROUP_SIZE)
            rows += table.num_rows

    print(f"  {rows:,} rows", flush=True)
    return output_path


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
    args = parser.parse_args(argv)

    input_dir = args.input_dir
    output_dir = args.output_dir or input_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    for name in TABLES:
        convert_table(name, input_dir, output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
