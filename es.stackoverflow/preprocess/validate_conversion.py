#!/usr/bin/env python3
"""Validate the CSV-to-Parquet conversion without loading whole tables.

The checks intentionally use two independent readers: Python's CSV reader for
the source and PyArrow's Parquet reader for the result.  Besides checking row
counts, the script compares null counts and simple numeric aggregates.  It
also checks the exact Arrow schema, malformed CSV rows, missing required
values, integer ranges, and duplicate primary keys.

Example:

    python3 validate_conversion.py --input-dir data --output-dir data
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

try:
    from .csvtoparquet import TABLES, TableSpec
except ImportError:
    from csvtoparquet import TABLES, TableSpec


CSV_FIELD_SIZE_LIMIT = 16 * 1024 * 1024
PARQUET_BATCH_SIZE = 100_000
MAX_REPORTED_ERRORS = 20

# These fields are useful sanity-check aggregates for the final report.  All
# integer columns are checked, but reporting every Id/FK mean is not useful.
SUMMARY_COLUMNS: dict[str, tuple[str, ...]] = {
    "Posts": ("AnswerCount", "CommentCount", "Score", "ViewCount"),
    "Votes": ("BountyAmount", "VoteTypeId"),
    "Users": ("DownVotes", "Reputation", "UpVotes", "Views"),
    "Tags": ("Count",),
    "Comments": ("Score",),
}


@dataclass
class NumericStats:
    count: int = 0
    total: int = 0
    minimum: int | None = None
    maximum: int | None = None

    def add(self, value: int) -> None:
        self.count += 1
        self.total += value
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)

    def extend(self, count: int, total: int, minimum: int, maximum: int) -> None:
        if count == 0:
            return
        self.count += count
        self.total += total
        self.minimum = minimum if self.minimum is None else min(self.minimum, minimum)
        self.maximum = maximum if self.maximum is None else max(self.maximum, maximum)

    @property
    def mean(self) -> float | None:
        return self.total / self.count if self.count else None


@dataclass
class SourceStats:
    row_count: int = 0
    empty_counts: dict[str, int] = field(default_factory=dict)
    numeric: dict[str, NumericStats] = field(default_factory=dict)
    vote_type_counts: Counter[int] = field(default_factory=Counter)


@dataclass
class ValidationResult:
    name: str
    csv: SourceStats | None = None
    parquet_rows: int | None = None
    parquet_null_counts: dict[str, int] | None = None
    parquet_numeric: dict[str, NumericStats] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def error(self, message: str) -> None:
        if len(self.errors) < MAX_REPORTED_ERRORS:
            self.errors.append(message)


def _is_integer(field: pa.Field) -> bool:
    return pa.types.is_integer(field.type)


def _integer_value(raw: str, field: pa.Field) -> int:
    value = int(raw)
    # Going through Arrow catches values outside int8/int32/int64 as well as
    # malformed values before they can contaminate the aggregate comparison.
    pa.scalar(value, type=field.type)
    return value


def _validate_csv(path: Path, spec: TableSpec, result: ValidationResult) -> SourceStats:
    stats = SourceStats(
        empty_counts={field.name: 0 for field in spec.schema},
        numeric={
            field.name: NumericStats()
            for field in spec.schema
            if _is_integer(field)
        },
    )

    if not path.is_file():
        result.error(f"CSV not found: {path}")
        return stats

    csv.field_size_limit(max(csv.field_size_limit(), CSV_FIELD_SIZE_LIMIT))
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.reader(source, strict=True)
            try:
                header = next(reader)
            except StopIteration:
                result.error(f"CSV is empty: {path}")
                return stats

            if len(header) != len(set(header)):
                result.error(f"CSV header contains duplicate columns: {path}")

            expected = set(spec.schema.names)
            actual = set(header)
            missing = [name for name in spec.schema.names if name not in actual]
            extra = [name for name in header if name not in expected]
            nullable = {
                field.name for field in spec.schema if field.nullable
            }
            allowed_missing = nullable | set(spec.zero_defaults)
            for name in missing:
                if name not in allowed_missing:
                    result.error(f"{path}: missing required column {name}")
            for name in extra:
                result.error(f"{path}: unexpected column {name}")

            positions = {name: index for index, name in enumerate(header)}
            fields = list(spec.schema)

            for row_number, row in enumerate(reader, start=2):
                stats.row_count += 1
                if len(row) != len(header):
                    result.error(
                        f"{path}:{row_number}: expected {len(header)} fields, "
                        f"found {len(row)}"
                    )
                    continue

                for field in fields:
                    name = field.name
                    position = positions.get(name)
                    if position is None:
                        # The missing column is materialized by the converter.
                        if name in spec.zero_defaults:
                            stats.numeric[name].add(spec.zero_defaults[name])
                        else:
                            stats.empty_counts[name] += 1
                        continue

                    raw = row[position]
                    if raw == "":
                        stats.empty_counts[name] += 1
                        if name in spec.zero_defaults:
                            stats.numeric[name].add(spec.zero_defaults[name])
                        elif not field.nullable:
                            result.error(
                                f"{path}:{row_number}: required field {name} is empty"
                            )
                        continue

                    if _is_integer(field):
                        try:
                            value = _integer_value(raw, field)
                        except (OverflowError, TypeError, ValueError, pa.ArrowException) as exc:
                            result.error(
                                f"{path}:{row_number}: invalid {name}={raw!r}: {exc}"
                            )
                            continue
                        stats.numeric[name].add(value)
                        if name == "VoteTypeId":
                            stats.vote_type_counts[value] += 1

    except (OSError, UnicodeError, csv.Error) as exc:
        result.error(f"Could not read {path}: {exc}")

    if stats.row_count == 0 and not result.errors:
        result.error(f"CSV has no data rows: {path}")
    return stats


def _parquet_null_counts(metadata: pq.FileMetaData, names: list[str]) -> dict[str, int] | None:
    indexes = {name: index for index, name in enumerate(names)}
    counts = {name: 0 for name in names}
    for row_group_index in range(metadata.num_row_groups):
        row_group = metadata.row_group(row_group_index)
        for name, index in indexes.items():
            statistics = row_group.column(index).statistics
            if statistics is None or statistics.null_count is None:
                return None
            counts[name] += statistics.null_count
    return counts


def _validate_parquet(path: Path, spec: TableSpec, result: ValidationResult) -> None:
    if not path.is_file():
        result.error(f"Parquet not found: {path}")
        return

    try:
        parquet = pq.ParquetFile(path)
        schema = parquet.schema_arrow
        schema_ok = schema.equals(spec.schema, check_metadata=True)
        if not schema_ok:
            result.error(f"{path}: Arrow schema differs from the declared schema")
        if "FavoriteCount" in schema.names:
            result.error(f"{path}: obsolete FavoriteCount column is present")

        metadata = parquet.metadata
        result.parquet_rows = metadata.num_rows
        if not schema_ok:
            return
        result.parquet_null_counts = _parquet_null_counts(metadata, spec.schema.names)

        integer_names = [
            field.name for field in spec.schema if _is_integer(field)
        ]
        numeric = {
            name: NumericStats() for name in integer_names
        }
        seen_ids: set[int] = set()
        for batch in parquet.iter_batches(
            batch_size=PARQUET_BATCH_SIZE,
            columns=integer_names,
            use_threads=True,
        ):
            for name, array in zip(integer_names, batch.columns):
                field = spec.schema.field(name)
                null_count = array.null_count
                if null_count and (not field.nullable or name in spec.zero_defaults):
                    result.error(
                        f"{path}: {name} contains {null_count} unexpected null values"
                    )

                values = pc.drop_null(array)
                if not len(values):
                    continue
                numeric[name].extend(
                    len(values),
                    int(pc.sum(values).as_py()),
                    pc.min(values).as_py(),
                    pc.max(values).as_py(),
                )
                if name == "Id":
                    for value in values.to_pylist():
                        value = int(value)
                        if value in seen_ids:
                            result.error(f"{path}: duplicate primary key Id={value}")
                        seen_ids.add(value)
        result.parquet_numeric = numeric
    except (OSError, pa.ArrowException, ValueError) as exc:
        result.error(f"Could not read {path}: {exc}")


def _expected_null_count(
    name: str,
    field: pa.Field,
    source: SourceStats,
    zero_defaults: Mapping[str, int],
) -> int:
    if name in zero_defaults:
        return 0
    if field.nullable:
        return source.empty_counts[name]
    return 0


def _compare(result: ValidationResult, spec: TableSpec) -> None:
    source = result.csv
    if source is None or result.parquet_rows is None:
        return

    if source.row_count != result.parquet_rows:
        result.error(
            f"row count differs: CSV={source.row_count}, "
            f"Parquet={result.parquet_rows}"
        )

    if result.parquet_null_counts is not None:
        for field in spec.schema:
            expected = _expected_null_count(
                field.name, field, source, spec.zero_defaults
            )
            actual = result.parquet_null_counts[field.name]
            if expected != actual:
                result.error(
                    f"{field.name} null count differs: expected={expected}, "
                    f"Parquet={actual}"
                )

    for name, expected in source.numeric.items():
        actual = result.parquet_numeric.get(name)
        if actual is None:
            continue
        values = (
            ("count", expected.count, actual.count),
            ("sum", expected.total, actual.total),
            ("min", expected.minimum, actual.minimum),
            ("max", expected.maximum, actual.maximum),
        )
        for label, expected_value, actual_value in values:
            if expected_value != actual_value:
                result.error(
                    f"{name} {label} differs: expected={expected_value}, "
                    f"Parquet={actual_value}"
                )


def _format_number(value: int | float | None) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, float):
        return f"{value:.3f}"
    return f"{value:,}"


def _print_result(result: ValidationResult, spec: TableSpec) -> None:
    source_rows = result.csv.row_count if result.csv else None
    print(
        f"{result.name}: CSV rows={source_rows:,}  "
        f"Parquet rows={result.parquet_rows:,}"
        if source_rows is not None and result.parquet_rows is not None
        else f"{result.name}: incomplete"
    )

    source = result.csv
    if source is not None:
        for name in SUMMARY_COLUMNS.get(result.name, ()):
            stats = source.numeric.get(name)
            if stats is None:
                continue
            print(
                f"  {name}: mean={_format_number(stats.mean)}, "
                f"sum={_format_number(stats.total)}, "
                f"min={_format_number(stats.minimum)}, "
                f"max={_format_number(stats.maximum)}"
            )
        if source.vote_type_counts:
            distribution = ", ".join(
                f"{key}={value:,}"
                for key, value in sorted(source.vote_type_counts.items())
            )
            print(f"  VoteTypeId counts: {distribution}")

    if result.errors:
        for error in result.errors:
            print(f"  ERROR: {error}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate prepared CSV files against their Parquet output."
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("data"),
        help="directory containing the CSV files (default: data)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="directory containing the Parquet files (default: input directory)",
    )
    args = parser.parse_args(argv)
    output_dir = args.output_dir or args.input_dir

    results: list[ValidationResult] = []
    for name, spec in TABLES.items():
        result = ValidationResult(name)
        result.csv = _validate_csv(args.input_dir / f"{name}.csv", spec, result)
        _validate_parquet(output_dir / f"{name}.parquet", spec, result)
        _compare(result, spec)
        results.append(result)
        _print_result(result, spec)

    errors = sum(len(result.errors) for result in results)
    if errors:
        print(f"Validation failed: {errors} error(s) reported.")
        return 1
    print("Validation OK: CSV and Parquet agree for all tables.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
