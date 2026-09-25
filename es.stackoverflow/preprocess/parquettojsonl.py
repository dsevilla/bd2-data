"""Build a reduced JSONL sample of the dump for in-browser practice.

The Parquet files published in the data release are the full dump: around a
gigabyte of data, 96% of which is the HTML in ``Posts.Body``.  A browser
cannot hold that as JavaScript objects, so this script writes a much smaller
sample that keeps the *shape* of the data intact and can be loaded in one go
by an in-memory MongoDB query engine such as mingo.

Two independent reductions are applied:

* **Long text is truncated** (``Body``, ``Text`` and ``AboutMe``) to
  ``--text-limit`` characters.  A truncated value ends with an ellipsis so a
  cut value is distinguishable from a naturally short one.
* **Rows are sampled by thread, not at random.**  A question is kept when
  ``Id % --thread-modulo == 0``; then *all* of its answers, comments and
  votes are kept, together with every user those rows reference.  Sampling
  whole threads is what keeps ``$lookup``, ``$group`` and the comment and
  vote counts consistent: a random sample of posts would leave answers
  without their question and joins returning empty arrays.  The ``Tags``
  table is always kept whole, as are the excerpt and wiki posts it points at.

The documents follow the same conventions as the session 3 and 4 notebooks of
the BDGE course, which load the Parquet files with
``RecordBatch.to_pylist()``: the column order of the Parquet file is kept,
every column is present in every document, and missing values are ``null``
rather than absent keys.  Dates are written in MongoDB Extended JSON (relaxed
mode), ``{"$date": "2008-09-15T08:09:02.123Z"}``, so the same file can be fed
to ``mongoimport`` and to a browser loader that revives those objects as
JavaScript ``Date`` values.  No ``_id`` is generated: as in the notebooks, the
server (or the loader) decides it.

Example:

    python3 parquettojsonl.py --input-dir data --output-dir data

The output directory receives one ``<Table>.jsonl.gz`` per table plus a
``manifest.json`` describing the sample.  The gzip stream is written with a
zero timestamp so that an unchanged input produces byte-identical output and
does not create empty commits.  Packaging is left to the CI workflow.
"""

from __future__ import annotations

import argparse
import gzip
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

# ---------------------------------------------------------------------------
# Sample definition
# ---------------------------------------------------------------------------

TABLES: tuple[str, ...] = ("Posts", "Users", "Comments", "Votes", "Tags")

# Columns whose value is cut to --text-limit characters.  These three hold
# 93% of the uncompressed dump between them.
TRUNCATED_COLUMNS: dict[str, tuple[str, ...]] = {
    "Posts": ("Body",),
    "Comments": ("Text",),
    "Users": ("AboutMe",),
}
TRUNCATION_MARK: str = "…"

QUESTION_POST_TYPE: int = 1

# The session 3 loader reads the Parquet files in batches of 20.000 rows.
BATCH_SIZE: int = 20_000

DEFAULT_THREAD_MODULO: int = 8
DEFAULT_TEXT_LIMIT: int = 100
DEFAULT_SOURCE_RELEASE: str = "es.stackoverflow.data-26-27"

# GitHub refuses pushes with files over 100 MB.  The sample is expected to be
# two orders of magnitude smaller than that; the check exists so that a future
# change of parameters fails here instead of at ``git push``.
MAX_FILE_BYTES: int = 95 * 1024 * 1024


@dataclass(frozen=True)
class TableReport:
    """What ended up in one output file."""

    documents: int
    jsonl_bytes: int
    gzip_bytes: int


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _parquet_path(input_dir: Path, table: str) -> Path:
    path: Path = input_dir / f"{table}.parquet"
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} not found: download the {table}.parquet asset of the data "
            "release first (the Makefile's download-parquet target does it)"
        )
    return path


def _iter_batches(
    path: Path,
    columns: list[str] | None,
    batch_size: int,
) -> Iterator[pa.RecordBatch]:
    """Stream a Parquet file, reading only the requested columns."""

    parquet: pq.ParquetFile = pq.ParquetFile(path)
    yield from parquet.iter_batches(
        batch_size=batch_size,
        columns=columns,
        use_threads=True,
    )


# ---------------------------------------------------------------------------
# Row selection
# ---------------------------------------------------------------------------


def select_questions(posts_path: Path, modulo: int, batch_size: int) -> set[int]:
    """Ids of the questions kept by the sample.

    Post ids are assigned in creation order, so taking one question out of
    every ``modulo`` spreads the sample evenly over the whole history of the
    site instead of concentrating it in one period.  The rule is a plain
    remainder so that regenerating the sample selects exactly the same
    threads.
    """

    questions: set[int] = set()
    for batch in _iter_batches(posts_path, ["Id", "PostTypeId"], batch_size):
        ids: list[int] = batch.column("Id").to_pylist()
        types: list[int] = batch.column("PostTypeId").to_pylist()
        questions.update(
            post_id
            for post_id, post_type in zip(ids, types)
            if post_type == QUESTION_POST_TYPE and post_id % modulo == 0
        )
    return questions


def select_answers(
    posts_path: Path,
    questions: set[int],
    batch_size: int,
) -> set[int]:
    """Ids of every answer belonging to a selected question."""

    answers: set[int] = set()
    for batch in _iter_batches(posts_path, ["Id", "ParentId"], batch_size):
        ids: list[int] = batch.column("Id").to_pylist()
        parents: list[int | None] = batch.column("ParentId").to_pylist()
        answers.update(
            post_id
            for post_id, parent in zip(ids, parents)
            if parent is not None and parent in questions
        )
    return answers


def select_tag_posts(tags_path: Path, batch_size: int) -> set[int]:
    """Ids of the excerpt and wiki posts referenced by the Tags table.

    Tags is kept whole, so the posts it points at are kept too; otherwise
    joining Tags with Posts would return empty arrays for every tag.
    """

    tag_posts: set[int] = set()
    for batch in _iter_batches(
        tags_path, ["ExcerptPostId", "WikiPostId"], batch_size
    ):
        for column in ("ExcerptPostId", "WikiPostId"):
            tag_posts.update(
                post_id
                for post_id in batch.column(column).to_pylist()
                if post_id is not None
            )
    return tag_posts


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _json_default(value: Any) -> Any:
    """Serialise the only non-JSON type Arrow produces here: timestamps."""

    if isinstance(value, datetime):
        moment: datetime = (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )
        stamp: str = moment.isoformat(timespec="milliseconds")
        return {"$date": stamp.replace("+00:00", "Z")}
    raise TypeError(f"unexpected value of type {type(value).__name__}")


def _truncate(value: str, limit: int) -> str:
    if limit <= 0 or len(value) <= limit:
        return value
    return value[:limit] + TRUNCATION_MARK


class _JsonlWriter:
    """Write documents as gzipped JSON Lines, counting both sizes."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._raw_file = path.open("wb")
        # mtime=0 keeps the output byte-identical for identical input.
        self._gzip_file = gzip.GzipFile(
            filename="", mode="wb", fileobj=self._raw_file, compresslevel=9, mtime=0
        )
        self.documents: int = 0
        self.jsonl_bytes: int = 0

    def write(self, document: dict[str, Any]) -> None:
        line: bytes = (
            json.dumps(
                document,
                default=_json_default,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        self._gzip_file.write(line)
        self.documents += 1
        self.jsonl_bytes += len(line)

    def abort(self) -> None:
        """Close the files without checking anything, to re-raise the error."""

        self._gzip_file.close()
        self._raw_file.close()

    def close(self) -> TableReport:
        self._gzip_file.close()
        self._raw_file.close()
        gzip_bytes: int = self._path.stat().st_size
        if gzip_bytes > MAX_FILE_BYTES:
            raise ValueError(
                f"{self._path.name} is {gzip_bytes / 1e6:,.0f} MB, over the "
                f"{MAX_FILE_BYTES / 1e6:,.0f} MB limit for a file in the "
                "repository: raise --thread-modulo or lower --text-limit"
            )
        return TableReport(
            documents=self.documents,
            jsonl_bytes=self.jsonl_bytes,
            gzip_bytes=gzip_bytes,
        )


def write_table(
    table: str,
    input_dir: Path,
    output_dir: Path,
    keep: set[int] | None,
    key_column: str,
    text_limit: int,
    batch_size: int,
    collect_users_from: tuple[str, ...] = (),
) -> tuple[TableReport, set[int]]:
    """Write one table's sampled rows, reporting the user ids they reference.

    ``keep`` is the set of accepted values for ``key_column``; ``None`` keeps
    every row.  The rows are filtered inside Arrow, before the batch is turned
    into Python dictionaries, so the discarded majority never becomes objects.
    """

    path: Path = _parquet_path(input_dir, table)
    truncated: tuple[str, ...] = TRUNCATED_COLUMNS.get(table, ())
    value_set: pa.Array | None = (
        pa.array(sorted(keep), type=pa.int64()) if keep is not None else None
    )
    user_ids: set[int] = set()
    writer = _JsonlWriter(output_dir / f"{table}.jsonl.gz")
    try:
        for batch in _iter_batches(path, None, batch_size):
            if value_set is not None:
                batch = batch.filter(
                    pc.is_in(batch.column(key_column), value_set=value_set)
                )
            if batch.num_rows == 0:
                continue
            for column in collect_users_from:
                user_ids.update(
                    user_id
                    for user_id in batch.column(column).to_pylist()
                    if user_id is not None
                )
            for document in cast(list[dict[str, Any]], batch.to_pylist()):
                for column in truncated:
                    text: str | None = document[column]
                    if text is not None:
                        document[column] = _truncate(text, text_limit)
                writer.write(document)
    except Exception:
        writer.abort()
        raise
    return writer.close(), user_ids


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def build_sample(
    input_dir: Path,
    output_dir: Path,
    thread_modulo: int,
    text_limit: int,
    batch_size: int = BATCH_SIZE,
    source_release: str = DEFAULT_SOURCE_RELEASE,
) -> dict[str, Any]:
    """Write every output file and return the manifest describing them."""

    posts_path: Path = _parquet_path(input_dir, "Posts")
    tags_path: Path = _parquet_path(input_dir, "Tags")

    questions: set[int] = select_questions(posts_path, thread_modulo, batch_size)
    answers: set[int] = select_answers(posts_path, questions, batch_size)
    tag_posts: set[int] = select_tag_posts(tags_path, batch_size)
    posts: set[int] = questions | answers | tag_posts
    print(
        f"Selected {len(questions):,} questions, {len(answers):,} answers and "
        f"{len(tag_posts - questions - answers):,} tag posts",
        flush=True,
    )

    reports: dict[str, TableReport] = {}
    users: set[int] = set()

    reports["Posts"], posts_users = write_table(
        "Posts",
        input_dir,
        output_dir,
        posts,
        "Id",
        text_limit,
        batch_size,
        collect_users_from=("OwnerUserId", "LastEditorUserId"),
    )
    users |= posts_users

    reports["Comments"], comments_users = write_table(
        "Comments",
        input_dir,
        output_dir,
        posts,
        "PostId",
        text_limit,
        batch_size,
        collect_users_from=("UserId",),
    )
    users |= comments_users

    reports["Votes"], votes_users = write_table(
        "Votes",
        input_dir,
        output_dir,
        posts,
        "PostId",
        text_limit,
        batch_size,
        collect_users_from=("UserId",),
    )
    users |= votes_users

    # Users is written last: it keeps exactly the users referenced by the rows
    # already written, so every OwnerUserId, LastEditorUserId and UserId in the
    # sample resolves to a document in this collection.
    reports["Users"], _ = write_table(
        "Users", input_dir, output_dir, users, "Id", text_limit, batch_size
    )
    reports["Tags"], _ = write_table(
        "Tags", input_dir, output_dir, None, "Id", text_limit, batch_size
    )

    manifest: dict[str, Any] = {
        "description": (
            "Muestra reducida del dump de es.stackoverflow para practicar "
            "consultas en el navegador. Se conservan hilos completos: cada "
            "pregunta viene con todas sus respuestas, comentarios, votos y "
            "los usuarios a los que esas filas hacen referencia."
        ),
        # Nothing here records when the job ran: identical inputs must give
        # byte-identical outputs, so that a rebuild that changes nothing
        # produces no commit at all.
        "source_release": source_release,
        "parameters": {
            "thread_modulo": thread_modulo,
            "text_limit": text_limit,
            "truncated_columns": {
                table: list(columns) for table, columns in TRUNCATED_COLUMNS.items()
            },
            "truncation_mark": TRUNCATION_MARK,
        },
        "format": {
            "type": "jsonl.gz",
            "encoding": "utf-8",
            "dates": "MongoDB Extended JSON (relaxed): {\"$date\": \"...\"}",
        },
        "collections": {
            table: {
                "file": f"{table}.jsonl.gz",
                "documents": reports[table].documents,
                "jsonl_bytes": reports[table].jsonl_bytes,
                "gzip_bytes": reports[table].gzip_bytes,
            }
            for table in TABLES
        },
    }
    manifest["totals"] = {
        "documents": sum(report.documents for report in reports.values()),
        "jsonl_bytes": sum(report.jsonl_bytes for report in reports.values()),
        "gzip_bytes": sum(report.gzip_bytes for report in reports.values()),
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build the reduced JSONL sample of the es.stackoverflow dump used "
            "for in-browser practice."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("data"),
        help="directory containing Posts.parquet, Users.parquet, ... (default: data)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="output directory (default: same as --input-dir)",
    )
    parser.add_argument(
        "--thread-modulo",
        type=int,
        default=DEFAULT_THREAD_MODULO,
        help=(
            "keep one question out of every N, with its whole thread "
            f"(default: {DEFAULT_THREAD_MODULO})"
        ),
    )
    parser.add_argument(
        "--text-limit",
        type=int,
        default=DEFAULT_TEXT_LIMIT,
        help=(
            "characters kept of Body, Text and AboutMe; 0 keeps them whole "
            f"(default: {DEFAULT_TEXT_LIMIT})"
        ),
    )
    parser.add_argument(
        "--source-release",
        default=DEFAULT_SOURCE_RELEASE,
        help=(
            "data release the Parquet files come from, recorded in the "
            f"manifest (default: {DEFAULT_SOURCE_RELEASE})"
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=BATCH_SIZE,
        help=f"rows read per Parquet batch (default: {BATCH_SIZE})",
    )
    args: argparse.Namespace = parser.parse_args(argv)

    input_dir: Path = cast(Path, args.input_dir)
    output_dir: Path = cast(Path | None, args.output_dir) or input_dir
    thread_modulo: int = cast(int, args.thread_modulo)
    text_limit: int = cast(int, args.text_limit)
    batch_size: int = cast(int, args.batch_size)
    if thread_modulo < 1:
        parser.error("--thread-modulo must be at least 1")
    if text_limit < 0:
        parser.error("--text-limit must not be negative")
    if batch_size < 1:
        parser.error("--batch-size must be at least 1")
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = build_sample(
        input_dir,
        output_dir,
        thread_modulo,
        text_limit,
        batch_size,
        cast(str, args.source_release),
    )
    for table in TABLES:
        entry: dict[str, Any] = manifest["collections"][table]
        print(
            f"{table + ':':<10} {entry['documents']:>9,} documents  "
            f"{entry['jsonl_bytes'] / 1e6:>8,.1f} MB JSONL  "
            f"{entry['gzip_bytes'] / 1e6:>7,.1f} MB gzip",
            flush=True,
        )
    totals: dict[str, Any] = manifest["totals"]
    print(
        f"{'total:':<10} {totals['documents']:>9,} documents  "
        f"{totals['jsonl_bytes'] / 1e6:>8,.1f} MB JSONL  "
        f"{totals['gzip_bytes'] / 1e6:>7,.1f} MB gzip",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
