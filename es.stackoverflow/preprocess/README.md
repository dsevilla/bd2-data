# Stack Exchange dump preprocessing

This directory converts selected files from the Stack Exchange public data dump
for Stack Overflow en español into CSV files used by the rest of this project.

## Source

The input is the public Stack Exchange data dump: a periodic snapshot of a
site's public data, distributed as XML files inside a 7-Zip archive. The list of
releases identifies **June 30, 2026** as the latest release available when this
file was written (2026-08-29). The pinned Spanish Stack Overflow archive is
recorded in [`URL`](URL):

<https://archive.org/download/stackexchange_20260630_sakura/stackexchange_20260630/es.stackoverflow.com.7z>

The file is hosted in the Internet Archive item
[`stackexchange_20260630_sakura`](https://archive.org/details/stackexchange_20260630_sakura),
a community re-upload used after the official archive stopped being available.
It is therefore worth recording the release date and checking the archive's
provenance when producing a reproducible dataset. The URL is intentionally
dated rather than an unversioned “latest” alias; it must be updated when a new
release appears.

Authoritative links:

- [All Stack Exchange data dump releases](https://meta.stackexchange.com/questions/224873/all-stack-exchange-data-dump-releases)
- [Database schema documentation for the public data dump and SEDE](https://meta.stackexchange.com/questions/2677/database-schema-documentation-for-the-public-data-dump-and-sede)
- [Stack Exchange Data Explorer](https://data.stackexchange.com/)
- [Creative Commons Attribution-ShareAlike license](https://creativecommons.org/licenses/by-sa/4.0/)
- [Python `xml.sax` documentation](https://docs.python.org/3/library/xml.sax.html)
- [Python `csv` documentation](https://docs.python.org/3/library/csv.html)

## Files and workflow

The current pipeline processes these five files:

| XML input | CSV output | Contents |
| --- | --- | --- |
| `Posts.xml` | `Posts.csv` | Questions and answers |
| `Votes.xml` | `Votes.csv` | Votes and other vote events |
| `Users.xml` | `Users.csv` | User records |
| `Tags.xml` | `Tags.csv` | Tag records |
| `Comments.xml` | `Comments.csv` | Comments on posts |

### CSV to Parquet

After the XML-to-CSV step, [`csvtoparquet.py`](csvtoparquet.py) applies the
explicit PyArrow schemas in its clearly marked `Explicit schemas` section and
writes one typed Parquet file per table. The schemas are based on the current
CSV headers and the official dump documentation, not on the old teaching
notebooks.

The conversion policy is:

- numeric counters and metrics whose missing value is treated as zero are
  filled with `0` and written as non-nullable columns;
- optional numeric foreign keys such as `ParentId` remain nullable;
- optional dates and strings remain nullable;
- legacy `Posts.FavoriteCount` is deliberately not generated: current public
  dumps no longer export it after Favorites/Bookmarks were replaced by
  private [Saves](https://meta.stackexchange.com/questions/383706/what-happened-to-favoritecount);
- the Arrow schema is stored inside each Parquet file.

The CSV reader is PyArrow's incremental `open_csv` reader with explicit column
types, so it processes batches rather than loading a complete table into
memory. The five independent tables are converted in parallel with Python's
`ProcessPoolExecutor`; the default is four workers per CPU, bounded by the
five available table tasks. This intentionally favors I/O overlap. Each worker
keeps the CSV reader single-threaded to avoid nested thread-pool
oversubscription. Parquet uses Brotli compression at level 11, dictionary
encoding, statistics, and the embedded Arrow schema.

Run it locally after generating the CSV files with:

```sh
python3 -m pip install -r requirements.txt
python3 csvtoparquet.py --input-dir data --output-dir data
# Optionally choose the number of table workers:
python3 csvtoparquet.py --input-dir data --output-dir data --workers 2
```

This produces only `Posts.parquet`, `Votes.parquet`, `Users.parquet`,
`Tags.parquet`, and `Comments.parquet`. The CI workflow packages those five
files as `es.stackoverflow.parquet.tar.gz` for distribution.

To check the conversion locally, run:

```sh
python3 validate_conversion.py --input-dir data --output-dir data
```

[`validate_conversion.py`](validate_conversion.py) reads the CSV and Parquet
independently and fails if it finds different row counts, malformed CSV rows,
an unexpected schema, unexpected nulls, invalid integer values, duplicate
primary keys, or different numeric counts/sums/minima/maxima. It also prints
means and the `VoteTypeId` distribution as a quick sanity report. It reads
only the relevant numeric columns from Parquet; the Parquet row count, schema,
and null counts come from its metadata where possible.

The archive also contains files such as `Badges.xml`, `PostHistory.xml`, and
`PostLinks.xml`; they are not currently converted by `preprocess.sh`.

Run the normal workflow from this directory:

```sh
cd preprocess
make
```

This builds the preprocessing container, downloads the archive from [`URL`](URL)
if the required XML files are missing, extracts the XML files directly into
`preprocess/data/`, and runs the five conversions. The generated
`preprocess/data/*.csv` files and the downloaded archive are ignored by Git.
The archive is large, so the download is reused on subsequent runs.
The Makefile creates `preprocess/data/` before Docker starts and checks that it
is writable by the current user.

The expected layout is `preprocess/data/Posts.xml`, not
`preprocess/data/<another-directory>/Posts.xml`. To deliberately download the
pinned URL again and overwrite the extracted files, run:

```sh
FORCE_DOWNLOAD=1 make
```

To disable automatic downloading and require pre-extracted XML files, run:

```sh
DOWNLOAD=0 make
```

The archive can also be downloaded and extracted manually if desired. In that
case, place the XML files directly under `preprocess/data/` and `make` will
skip the download.

For a local conversion without Docker, the converter can also be called as:

```sh
python3 xmltocsv.py data/Posts.xml data/Posts.csv
```

Omit the second argument to write CSV to standard output.

The workflow [`prepare-es-stackoverflow-data.yml`](../../.github/workflows/prepare-es-stackoverflow-data.yml)
performs the download, XML-to-CSV conversion, CSV gzip compression, and
CSV-to-Parquet conversion. It publishes the uncompressed CSV archive, every
individual `.csv.gz`, every Parquet file, and the complete Parquet tarball as
release assets.

## Conversion behavior

`xmltocsv.py` makes two streaming SAX passes over each XML file:

1. `schemaextract.py` collects the union of all attributes found on `<row>`
   elements.
2. `rowselector.py` writes one CSV header and one CSV row per `<row>` element.

Consequences of this design:

- An attribute absent from a particular XML row becomes an empty CSV field.
- The header is stable: `Id` is first when present and the other names are
  sorted case-insensitively.
- CSV quoting is handled by Python's `csv` module.
- XML entities are decoded by the SAX parser. For example, `&amp;` becomes `&`
  in the CSV value; it is not an extra application-level “unescape”.
- Input and output are explicitly UTF-8.
- Newlines inside field values are changed to `<br/>` for compatibility with
  consumers that cannot handle embedded newlines in quoted CSV fields. This is
  a representation change, so code that needs the original rendered text
  should read the XML dump directly.
- There is currently no date, user, post-type, or synthetic-row filter. The
  converter is intended to preserve the dump's rows and let later analysis
  choose its population.

The official schema documentation is the canonical reference because fields
change over time and nullable fields are omitted from individual XML rows. In
particular, `Posts.Body` is rendered HTML, not the original Markdown. Current
documentation also includes `ContentLicense` where applicable, while some
legacy fields described by older local notes—such as `Posts.DeletionDate`,
`Users.Age`, and `Users.EmailHash`—should not be assumed to exist in a current
public dump. `PostTypeId` also has values beyond question (`1`) and answer
(`2`), including wiki-related and moderation-related records.

## The two synthetic `Posts.xml` rows

Beginning with the July 2025 dump generation, Stack Exchange added two
artificial rows to each site's `Posts.xml`, including
`es.stackoverflow.com`. The official schema documentation identifies their
fixed IDs as:

- `1000000001`: a fabricated question (`PostTypeId=1`)
- `1000000010`: a fabricated answer (`PostTypeId=2`)

They are associated with the Community user (`OwnerUserId=-1`). The text is
generated for each site, so the Spanish dump's title and body can differ from
the English Stack Overflow dump, and can look like a normal question-and-answer
pair, but it is intentionally fabricated. These are not posts written by
Spanish Stack Overflow users and should not be interpreted as real questions,
answers, votes, or engagement.

This is intentional behavior of the dump, not an XML parsing error. In the
[staff explanation](https://meta.stackexchange.com/questions/412018/fabricated-data-in-posts-xml-for-multiple-all-data-dumps/412182), Stack Exchange
describes the rows as a deliberate watermark related to security and safety
and intended to discourage reuse of dump data in ways that could mislead the
community. Explanations beyond that staff statement are speculation and are
not treated as facts here.

The current converter keeps these rows, because it currently copies every
`<row>` and preserving the source is the least surprising default. To inspect
them after extraction:

```sh
rg 'Id="1000000001"|Id="1000000010"' data/Posts.xml
```

There are two reasonable later choices:

- **Keep them:** retain dump fidelity and exclude the two IDs explicitly in
  analytical queries when measuring real user activity.
- **Filter them during preprocessing:** produce a cleaned analytical dataset,
  but record that it is derived from the dump and that these IDs were removed.

If filtering is chosen, use the two documented IDs rather than filtering every
row owned by `-1`; the Community user also represents legitimate system and
community activity.

## Code layout

- [`preprocess.sh`](preprocess.sh) runs the five conversions in parallel.
- [`xmltocsv.py`](xmltocsv.py) handles command-line arguments and the two-pass
  conversion.
- [`schemaextract.py`](schemaextract.py) discovers the file-wide attribute
  schema.
- [`rowselector.py`](rowselector.py) writes the CSV header and rows.
- [`csvtoparquet.py`](csvtoparquet.py) applies the explicit schemas and writes
  typed Parquet files.
- [`validate_conversion.py`](validate_conversion.py) checks the CSV/Parquet
  row counts, schemas, nulls, keys, and numeric summaries.
- [`Dockerfile`](Dockerfile) supplies Python and GNU Parallel.
- [`URL`](URL) pins the source archive used for a run.
