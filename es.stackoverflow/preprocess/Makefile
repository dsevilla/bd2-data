all:
	@mkdir -p data
	@if [ ! -w data ]; then \
		echo "preprocess/data is not writable by the current user" >&2; \
		echo "Fix its ownership/permissions, then run make again." >&2; \
		exit 1; \
	fi
	DUID=$$(id -u) DGID=$$(id -g) DOWNLOAD=$(if $(DOWNLOAD),$(DOWNLOAD),1) FORCE_DOWNLOAD=$(if $(FORCE_DOWNLOAD),$(FORCE_DOWNLOAD),0) docker compose up --build

.PHONY: all
