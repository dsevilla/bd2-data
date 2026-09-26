"""Build complete or thread-sampled JSONL for the es.stackoverflow practice.

The default keeps every Parquet row. With ``--sample``, it keeps complete
question threads selected by Id modulo. In both modes, Posts.Body,
Comments.Text and Users.AboutMe are limited to 100 UTF-8 bytes by default.
Outputs are deterministic gzip JSONL.
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

TABLES: tuple[str, ...] = ("Posts", "Users", "Comments", "Votes", "Tags")
TRUNCATED_COLUMNS: dict[str, tuple[str, ...]] = {
    "Posts": ("Body",),
    "Comments": ("Text",),
    "Users": ("AboutMe",),
}
BATCH_SIZE: int = 20_000
DEFAULT_TEXT_LIMIT_BYTES: int = 100
DEFAULT_THREAD_MODULO: int = 8
DEFAULT_SOURCE_RELEASE: str = "es.stackoverflow.data-26-27"
QUESTION_POST_TYPE: int = 1
# Leave a margin below GitHub's 100 MB per-file limit.
MAX_FILE_BYTES: int = 95 * 1024 * 1024


@dataclass(frozen=True)
class TableReport:
    documents: int
    jsonl_bytes: int
    gzip_bytes: int


def _parquet_path(input_dir: Path, table: str) -> Path:
    path = input_dir / f"{table}.parquet"
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} not found: download the {table}.parquet asset of the data "
            "release first (the Makefile's download-parquet target does it)"
        )
    return path


def _iter_batches(
    path: Path,
    batch_size: int,
    columns: list[str] | None = None,
) -> Iterator[pa.RecordBatch]:
    parquet = pq.ParquetFile(path)
    yield from parquet.iter_batches(
        batch_size=batch_size, columns=columns, use_threads=True
    )


def _truncate_utf8(value: str, limit_bytes: int) -> str:
    """Keep a valid UTF-8 prefix no longer than limit_bytes."""
    encoded = value.encode("utf-8")
    if len(encoded) <= limit_bytes:
        return value
    return encoded[:limit_bytes].decode("utf-8", errors="ignore")


def select_questions(posts_path: Path, modulo: int, batch_size: int) -> set[int]:
    """Select every Nth question by Id, consistently across regenerations."""
    questions: set[int] = set()
    for batch in _iter_batches(posts_path, batch_size, ["Id", "PostTypeId"]):
        ids = batch.column("Id").to_pylist()
        types = batch.column("PostTypeId").to_pylist()
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
    """Keep every answer whose parent question belongs to the sample."""
    answers: set[int] = set()
    for batch in _iter_batches(posts_path, batch_size, ["Id", "ParentId"]):
        ids = batch.column("Id").to_pylist()
        parents = batch.column("ParentId").to_pylist()
        answers.update(
            post_id
            for post_id, parent in zip(ids, parents)
            if parent is not None and parent in questions
        )
    return answers


def select_tag_posts(tags_path: Path, batch_size: int) -> set[int]:
    """Keep the excerpt and wiki posts referenced by the complete Tags table."""
    tag_posts: set[int] = set()
    for batch in _iter_batches(
        tags_path, batch_size, ["ExcerptPostId", "WikiPostId"]
    ):
        for column in ("ExcerptPostId", "WikiPostId"):
            tag_posts.update(
                post_id
                for post_id in batch.column(column).to_pylist()
                if post_id is not None
            )
    return tag_posts


def _json_default(value: Any) -> Any:
    """Serialise Arrow timestamps as MongoDB Extended JSON relaxed dates."""
    if isinstance(value, datetime):
        moment = (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )
        stamp = moment.isoformat(timespec="milliseconds")
        return {"$date": stamp.replace("+00:00", "Z")}
    raise TypeError(f"unexpected value of type {type(value).__name__}")


class _JsonlWriter:
    """Write documents as deterministic, maximum-compression gzip JSONL."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._raw_file = path.open("wb")
        self._gzip_file = gzip.GzipFile(
            filename="", mode="wb", fileobj=self._raw_file,
            compresslevel=9, mtime=0,
        )
        self.documents = 0
        self.jsonl_bytes = 0

    def write(self, document: dict[str, Any]) -> None:
        line = (
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
        self._gzip_file.close()
        self._raw_file.close()

    def close(self) -> TableReport:
        self._gzip_file.close()
        self._raw_file.close()
        return TableReport(
            documents=self.documents,
            jsonl_bytes=self.jsonl_bytes,
            gzip_bytes=self._path.stat().st_size,
        )


def write_table(
    table: str,
    input_dir: Path,
    output_dir: Path,
    text_limit_bytes: int,
    batch_size: int,
) -> TableReport:
    """Write every row from one Parquet table, truncating only long text."""
    path = _parquet_path(input_dir, table)
    output_path = output_dir / f"{table}.jsonl.gz"
    temporary_path = output_dir / f"{table}.jsonl.gz.tmp"
    writer = _JsonlWriter(temporary_path)
    try:
        truncated = TRUNCATED_COLUMNS.get(table, ())
        for batch in _iter_batches(path, batch_size):
            for document in cast(list[dict[str, Any]], batch.to_pylist()):
                for column in truncated:
                    text = document[column]
                    if text is not None:
                        document[column] = _truncate_utf8(text, text_limit_bytes)
                writer.write(document)
        report = writer.close()
        if report.gzip_bytes > MAX_FILE_BYTES:
            raise ValueError(
                f"{output_path.name} is {report.gzip_bytes / 1e6:,.1f} MB, over "
                f"the safety limit of {MAX_FILE_BYTES / 1e6:,.1f} MB; use a "
                "more compact format or adjust the text limit"
            )
        temporary_path.replace(output_path)
        return report
    except Exception:
        writer.abort()
        temporary_path.unlink(missing_ok=True)
        raise


def write_sample_table(
    table: str,
    input_dir: Path,
    output_dir: Path,
    keep: set[int] | None,
    key_column: str,
    text_limit_bytes: int,
    batch_size: int,
    collect_users_from: tuple[str, ...] = (),
) -> tuple[TableReport, set[int]]:
    """Write a sampled table and collect user references from selected rows."""
    path = _parquet_path(input_dir, table)
    output_path = output_dir / f"{table}-sample.jsonl.gz"
    temporary_path = output_dir / f"{table}-sample.jsonl.gz.tmp"
    value_set = pa.array(sorted(keep), type=pa.int64()) if keep is not None else None
    users: set[int] = set()
    writer = _JsonlWriter(temporary_path)
    try:
        truncated = TRUNCATED_COLUMNS.get(table, ())
        for batch in _iter_batches(path, batch_size):
            if value_set is not None:
                batch = batch.filter(
                    pc.is_in(batch.column(key_column), value_set=value_set)
                )
            if batch.num_rows == 0:
                continue
            for column in collect_users_from:
                users.update(
                    user_id
                    for user_id in batch.column(column).to_pylist()
                    if user_id is not None
                )
            for document in cast(list[dict[str, Any]], batch.to_pylist()):
                for column in truncated:
                    text = document[column]
                    if text is not None:
                        document[column] = _truncate_utf8(text, text_limit_bytes)
                writer.write(document)
        report = writer.close()
        if report.gzip_bytes > MAX_FILE_BYTES:
            raise ValueError(
                f"{output_path.name} is {report.gzip_bytes / 1e6:,.1f} MB, over "
                f"the safety limit of {MAX_FILE_BYTES / 1e6:,.1f} MB"
            )
        temporary_path.replace(output_path)
        return report, users
    except Exception:
        writer.abort()
        temporary_path.unlink(missing_ok=True)
        raise


def _parameters(text_limit_bytes: int) -> dict[str, Any]:
    return {
        "sampling": "none; all rows",
        "text_limit_bytes": text_limit_bytes,
        "truncated_columns": {
            table: list(columns) for table, columns in TRUNCATED_COLUMNS.items()
        },
    }


def _sample_parameters(
    thread_modulo: int,
    text_limit_bytes: int,
) -> dict[str, Any]:
    return {
        "sampling": "every Nth question, with complete threads",
        "thread_modulo": thread_modulo,
        "text_limit_bytes": text_limit_bytes,
        "truncated_columns": {
            table: list(columns) for table, columns in TRUNCATED_COLUMNS.items()
        },
    }


def read_source(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def is_up_to_date(
    output_dir: Path,
    source: dict[str, Any] | None,
    text_limit_bytes: int,
) -> bool:
    """Check origin, full-data parameters, and every committed file size."""
    if source is None:
        return False
    try:
        manifest = json.loads((output_dir / "manifest.json").read_text("utf-8"))
        collections: dict[str, Any] = manifest["collections"]
        if manifest.get("source") != source:
            return False
        if manifest.get("parameters") != _parameters(text_limit_bytes):
            return False
        if set(collections) != set(TABLES):
            return False
        for table in TABLES:
            entry = collections[table]
            path = output_dir / entry["file"]
            if not path.is_file() or path.stat().st_size != entry["gzip_bytes"]:
                return False
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return True


def is_sample_up_to_date(
    output_dir: Path,
    source: dict[str, Any] | None,
    thread_modulo: int,
    text_limit_bytes: int,
) -> bool:
    """Check origin, sampling parameters, and all reduced files."""
    if source is None:
        return False
    try:
        manifest = json.loads(
            (output_dir / "manifest-sample.json").read_text("utf-8")
        )
        collections: dict[str, Any] = manifest["collections"]
        if manifest.get("source") != source:
            return False
        if manifest.get("parameters") != _sample_parameters(
            thread_modulo, text_limit_bytes
        ):
            return False
        if set(collections) != set(TABLES):
            return False
        for table in TABLES:
            entry = collections[table]
            path = output_dir / entry["file"]
            if not path.is_file() or path.stat().st_size != entry["gzip_bytes"]:
                return False
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return True


def build_jsonl(
    input_dir: Path,
    output_dir: Path,
    text_limit_bytes: int,
    batch_size: int = BATCH_SIZE,
    source_release: str = DEFAULT_SOURCE_RELEASE,
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reports: dict[str, TableReport] = {}
    for table in TABLES:
        reports[table] = write_table(
            table, input_dir, output_dir, text_limit_bytes, batch_size
        )
    manifest: dict[str, Any] = {
        "description": (
            "Todas las filas del dump de es.stackoverflow para practicar en el "
            "navegador. Body, Text y AboutMe se limitan a un máximo de "
            f"{text_limit_bytes} bytes UTF-8."
        ),
        "source_release": source_release,
        "source": source,
        "parameters": _parameters(text_limit_bytes),
        "format": {
            "type": "jsonl.gz",
            "encoding": "utf-8",
            "dates": "MongoDB Extended JSON (relaxed): {\"$date\": \"...\"}",
            "compression": "gzip level 9",
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
    temporary_manifest = output_dir / "manifest.json.tmp"
    temporary_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_manifest.replace(output_dir / "manifest.json")
    return manifest


def build_sample(
    input_dir: Path,
    output_dir: Path,
    thread_modulo: int,
    text_limit_bytes: int,
    batch_size: int = BATCH_SIZE,
    source_release: str = DEFAULT_SOURCE_RELEASE,
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a thread-complete sample alongside the full JSONL files."""
    posts_path = _parquet_path(input_dir, "Posts")
    tags_path = _parquet_path(input_dir, "Tags")
    questions = select_questions(posts_path, thread_modulo, batch_size)
    answers = select_answers(posts_path, questions, batch_size)
    tag_posts = select_tag_posts(tags_path, batch_size)
    posts = questions | answers | tag_posts
    print(
        f"Sample: {len(questions):,} questions, {len(answers):,} answers, "
        f"{len(tag_posts - questions - answers):,} tag excerpt/wiki posts",
        flush=True,
    )

    reports: dict[str, TableReport] = {}
    users: set[int] = set()
    reports["Posts"], referenced_users = write_sample_table(
        "Posts", input_dir, output_dir, posts, "Id", text_limit_bytes,
        batch_size, collect_users_from=("OwnerUserId", "LastEditorUserId"),
    )
    users |= referenced_users
    reports["Comments"], referenced_users = write_sample_table(
        "Comments", input_dir, output_dir, posts, "PostId", text_limit_bytes,
        batch_size, collect_users_from=("UserId",),
    )
    users |= referenced_users
    reports["Votes"], referenced_users = write_sample_table(
        "Votes", input_dir, output_dir, posts, "PostId", text_limit_bytes,
        batch_size, collect_users_from=("UserId",),
    )
    users |= referenced_users
    reports["Users"], _ = write_sample_table(
        "Users", input_dir, output_dir, users, "Id", text_limit_bytes, batch_size
    )
    reports["Tags"], _ = write_sample_table(
        "Tags", input_dir, output_dir, None, "Id", text_limit_bytes, batch_size
    )

    manifest: dict[str, Any] = {
        "description": (
            "Muestra reducida del mismo dump: una pregunta de cada "
            f"{thread_modulo}, con sus respuestas, comentarios, votos y usuarios "
            f"relacionados. Los campos de texto se limitan a {text_limit_bytes} "
            "bytes UTF-8."
        ),
        "source_release": source_release,
        "source": source,
        "parameters": _sample_parameters(thread_modulo, text_limit_bytes),
        "format": {
            "type": "jsonl.gz",
            "encoding": "utf-8",
            "dates": "MongoDB Extended JSON (relaxed): {\"$date\": \"...\"}",
            "compression": "gzip level 9",
        },
        "collections": {
            table: {
                "file": f"{table}-sample.jsonl.gz",
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
    temporary_manifest = output_dir / "manifest-sample.json.tmp"
    temporary_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_manifest.replace(output_dir / "manifest-sample.json")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build complete or thread-sampled JSONL data for the "
            "es.stackoverflow browser practice."
        )
    )
    parser.add_argument(
        "--input-dir", type=Path, default=Path("data"),
        help="directory containing Posts.parquet, Users.parquet, ... (default: data)",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=None,
        help="output directory (default: same as --input-dir)",
    )
    parser.add_argument(
        "--text-limit-bytes", type=int, default=DEFAULT_TEXT_LIMIT_BYTES,
        help=("maximum UTF-8 bytes retained in Body, Text and AboutMe; "
              f"default: {DEFAULT_TEXT_LIMIT_BYTES}"),
    )
    parser.add_argument(
        "--sample", action="store_true",
        help="build the thread-complete reduced dataset instead of all rows",
    )
    parser.add_argument(
        "--thread-modulo", type=int, default=DEFAULT_THREAD_MODULO,
        help=("in sample mode, keep every Nth question and its full thread; "
              f"default: {DEFAULT_THREAD_MODULO}"),
    )
    parser.add_argument(
        "--source-release", default=DEFAULT_SOURCE_RELEASE,
        help=f"data release recorded in the manifest (default: {DEFAULT_SOURCE_RELEASE})",
    )
    parser.add_argument(
        "--source-json", type=Path, default=None,
        help="source.json identifying the dump used to build the release",
    )
    parser.add_argument("--force", action="store_true", help="rebuild even if up to date")
    parser.add_argument(
        "--batch-size", type=int, default=BATCH_SIZE,
        help=f"rows read per Parquet batch (default: {BATCH_SIZE})",
    )
    args = parser.parse_args(argv)

    input_dir = cast(Path, args.input_dir)
    output_dir = cast(Path | None, args.output_dir) or input_dir
    text_limit_bytes = cast(int, args.text_limit_bytes)
    batch_size = cast(int, args.batch_size)
    thread_modulo = cast(int, args.thread_modulo)
    if text_limit_bytes < 0:
        parser.error("--text-limit-bytes must not be negative")
    if thread_modulo < 1:
        parser.error("--thread-modulo must be at least 1")
    if batch_size < 1:
        parser.error("--batch-size must be at least 1")
    output_dir.mkdir(parents=True, exist_ok=True)

    source = read_source(cast(Path | None, args.source_json))
    if source is None:
        print("warning: no source.json; the origin is unknown", flush=True)
    sample = cast(bool, args.sample)
    if sample:
        up_to_date = is_sample_up_to_date(
            output_dir, source, thread_modulo, text_limit_bytes
        )
    else:
        up_to_date = is_up_to_date(output_dir, source, text_limit_bytes)
    if not args.force and up_to_date:
        variant = "sample" if sample else "complete data"
        print(f"{output_dir}: {variant} already built from this source; nothing to do")
        return 0

    if sample:
        manifest = build_sample(
            input_dir,
            output_dir,
            thread_modulo,
            text_limit_bytes,
            batch_size,
            cast(str, args.source_release),
            source,
        )
    else:
        manifest = build_jsonl(
            input_dir,
            output_dir,
            text_limit_bytes,
            batch_size,
            cast(str, args.source_release),
            source,
        )
    for table in TABLES:
        entry = manifest["collections"][table]
        print(
            f"{table + ':':<10} {entry['documents']:>9,} documents  "
            f"{entry['jsonl_bytes'] / 1e6:>8,.1f} MB JSONL  "
            f"{entry['gzip_bytes'] / 1e6:>7,.1f} MB gzip",
            flush=True,
        )
    totals = manifest["totals"]
    print(
        f"{'total:':<10} {totals['documents']:>9,} documents  "
        f"{totals['jsonl_bytes'] / 1e6:>8,.1f} MB JSONL  "
        f"{totals['gzip_bytes'] / 1e6:>7,.1f} MB gzip",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
