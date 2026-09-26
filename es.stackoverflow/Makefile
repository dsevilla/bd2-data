# Rebuild the artefacts derived from the 26-27 data release.  Run from this
# directory:
#
#   make          the SQLite backup fragments (es.stackoverflow.db.xz.00/.01)
#   make jsonl    the reduced JSONL sample for in-browser practice (jsonl/)
#   make parquet  the Parquet files served from the repository (parquet/)
#
# All start from the assets published in the release, not from the files
# checked into this repository.  parquet.old/ is the previous course's
# generator and data; nothing here reads or writes it.

REPOSITORY ?= dsevilla/bd2-data
RELEASE_TAG ?= es.stackoverflow.data-26-27
CSV_ARCHIVE := preprocess/es.stackoverflow.csv.tar.gz
CSV_URL := https://github.com/$(REPOSITORY)/releases/download/$(RELEASE_TAG)/es.stackoverflow.csv.tar.gz
CSV_DIR := preprocess/data
CSV_NAMES := Posts.csv Votes.csv Users.csv Tags.csv Comments.csv
DB := es.stackoverflow.db
DB_TMP := $(DB).tmp
XZ_FILE := $(DB).xz
PART_00 := $(XZ_FILE).00
PART_01 := $(XZ_FILE).01
SPLIT_PREFIX := $(XZ_FILE).part.

# The 512 MiB LZMA2 dictionary and maximum match-finder settings produced the
# smallest stream tested for this database. Each split is capped at 99 MiB.
XZ_OPTIONS := --check=crc64 --threads=1 --lzma2=dict=512MiB,lc=3,lp=0,pb=2,mode=normal,nice=273,mf=bt4,depth=0
SPLIT_BYTES := 103809024

# --- Reduced JSONL sample -------------------------------------------------
# One question out of every THREAD_MODULO is kept, together with its whole
# thread; Body, Text and AboutMe are cut to TEXT_LIMIT characters.  See
# preprocess/parquettojsonl.py and jsonl/README.md.
PYTHON ?= python3
PARQUET_NAMES := Posts.parquet Users.parquet Comments.parquet Votes.parquet Tags.parquet
PARQUET_DIR := preprocess/data
PARQUET_FILES := $(addprefix $(PARQUET_DIR)/,$(PARQUET_NAMES))
PARQUET_URL_BASE := https://github.com/$(REPOSITORY)/releases/download/$(RELEASE_TAG)
JSONL_DIR := jsonl
REPO_PARQUET_DIR := parquet
THREAD_MODULO ?= 8
TEXT_LIMIT ?= 100

.PHONY: all download extract-csv database compress split \
	download-parquet parquet jsonl clean-build

all: split

download: $(CSV_ARCHIVE)

$(CSV_ARCHIVE):
	mkdir -p "$(dir $@)"
	curl --fail --location --retry 3 --retry-delay 5 --output "$@.part" "$(CSV_URL)"
	mv -f "$@.part" "$@"

extract-csv: $(CSV_ARCHIVE)
	mkdir -p "$(CSV_DIR)"
	tar -xzf "$(CSV_ARCHIVE)" -C "$(CSV_DIR)" $(CSV_NAMES)
	@set -eu; for file in $(addprefix $(CSV_DIR)/,$(CSV_NAMES)); do \
		test -s "$$file" || { echo "Missing or empty CSV: $$file" >&2; exit 1; }; \
	done

database: extract-csv
	rm -f "$(DB_TMP)"
	cd "$(CSV_DIR)" && sqlite3 -bail "$(abspath $(DB_TMP))" < "$(abspath sqlite3-import.sql)"
	mv -f "$(DB_TMP)" "$(DB)"

compress: database
	rm -f "$(XZ_FILE).tmp"
	xz $(XZ_OPTIONS) -c "$(DB)" > "$(XZ_FILE).tmp"
	xz -t "$(XZ_FILE).tmp"
	mv -f "$(XZ_FILE).tmp" "$(XZ_FILE)"

split: compress
	@set -eu; \
	  rm -f "$(SPLIT_PREFIX)"*; \
	  split -b $(SPLIT_BYTES) -a 2 "$(XZ_FILE)" "$(SPLIT_PREFIX)"; \
	  test -s "$(SPLIT_PREFIX)aa"; \
	  test -s "$(SPLIT_PREFIX)ab"; \
	  if test -e "$(SPLIT_PREFIX)ac"; then \
	    echo "Compressed database needs more than two fragments; refusing to replace the checked-in pair." >&2; \
	    exit 1; \
	  fi; \
	  cat "$(SPLIT_PREFIX)aa" "$(SPLIT_PREFIX)ab" | xz -t; \
	  mv -f "$(SPLIT_PREFIX)aa" "$(PART_00)"; \
	  mv -f "$(SPLIT_PREFIX)ab" "$(PART_01)"; \
	  test "$$(wc -c < "$(PART_00)")" -le $(SPLIT_BYTES); \
	  test "$$(wc -c < "$(PART_01)")" -le $(SPLIT_BYTES)

download-parquet: $(PARQUET_FILES)

$(PARQUET_DIR)/%.parquet:
	mkdir -p "$(dir $@)"
	curl --fail --location --retry 3 --retry-delay 5 --output "$@.part" "$(PARQUET_URL_BASE)/$(notdir $@)"
	mv -f "$@.part" "$@"

# The release assets, ready to be committed: Posts.parquet is split in pieces
# under GitHub's 100 MB limit, the other tables are copied as they are.  They
# live in the repository because release assets are sent without CORS headers
# and web pages cannot fetch them; see preprocess/splitparquet.py.
parquet: $(PARQUET_FILES)
	$(PYTHON) preprocess/splitparquet.py \
	  --input-dir "$(PARQUET_DIR)" \
	  --output-dir "$(REPO_PARQUET_DIR)"

# One run of the script writes the five .jsonl.gz files and the manifest, so
# this is a phony target instead of one rule per output file.  The script
# fails if a file grows past the size limit for a file in the repository.
jsonl: $(PARQUET_FILES)
	mkdir -p "$(JSONL_DIR)"
	$(PYTHON) preprocess/parquettojsonl.py \
	  --input-dir "$(PARQUET_DIR)" \
	  --output-dir "$(JSONL_DIR)" \
	  --thread-modulo $(THREAD_MODULO) \
	  --text-limit $(TEXT_LIMIT) \
	  --source-release "$(RELEASE_TAG)"

# Remove downloaded/extracted source and build intermediates, preserving the
# two final .db.xz.00/.01 files, the JSONL sample and parquet/.
clean-build:
	rm -f "$(CSV_ARCHIVE)" "$(CSV_ARCHIVE).part" "$(DB)" "$(DB_TMP)" \
	  "$(XZ_FILE)" "$(XZ_FILE).tmp" "$(SPLIT_PREFIX)"*
	@for file in $(addprefix $(CSV_DIR)/,$(CSV_NAMES)); do rm -f "$$file"; done
	rm -f $(PARQUET_FILES) $(addsuffix .part,$(PARQUET_FILES))
