FROM prom/prometheus:latest

# Install AWS CLI for S3 config sync (runs as root during build only)
USER root
RUN apk add --no-cache aws-cli
USER nobody

COPY entrypoint.sh /bin/entrypoint.sh

ENTRYPOINT ["/bin/entrypoint.sh"]
