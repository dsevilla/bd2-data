FROM alpine:latest

# Install tini
RUN apk add --no-cache tini

# Create data directory
RUN mkdir -p /data

# Copy SQL files
COPY es.stackoverflow.sql.gz /data/

# Use tini as entrypoint
ENTRYPOINT ["/sbin/tini", "--"]

# Keep container running (sleep infinity)
CMD ["sleep", "infinity"]
