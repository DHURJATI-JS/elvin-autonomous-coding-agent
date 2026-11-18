FROM clamav/clamav:1.5.4-debian
# allow scanning files up to 100 MB through clamd's INSTREAM command
RUN f=/etc/clamav/clamd.conf; sed -i -E 's/^#?[[:space:]]*StreamMaxLength.*/StreamMaxLength 100M/' "$f"; \
    grep -q '^StreamMaxLength' "$f" || echo 'StreamMaxLength 100M' >> "$f"; grep -E '^(StreamMaxLength|TCPSocket)' "$f" || true
