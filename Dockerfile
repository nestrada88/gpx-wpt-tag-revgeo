# syntax=docker/dockerfile:1

# Python 3.13 is compatible with the script's type syntax and current runtime
# dependencies. The slim Debian base keeps the runtime image small while
# retaining standard CA certificates required for HTTPS reverse geocoding.
FROM python:3.13-slim-bookworm

LABEL org.opencontainers.image.title="Trail One GPX Trail Waypoint Generator" \
      org.opencontainers.image.description="Generate GPX trail waypoints with elevation metrics and Nominatim reverse geocoding." \
      org.opencontainers.image.licenses="UNSPECIFIED"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Keep application code separate from user-provided GPX data.
WORKDIR /app

# Install Python dependencies before copying the application so dependency
# layers remain cacheable when only the script changes.
COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir --requirement requirements.txt

COPY gpx_wpt_tag_revgeo.py ./gpx_wpt_tag_revgeo.py

# Run as a dedicated unprivileged account. The fixed UID/GID makes ownership
# predictable when mapping a writable host directory into /data.
RUN groupadd --system --gid 10001 trailone \
    && useradd --system \
        --uid 10001 \
        --gid 10001 \
        --home-dir /nonexistent \
        --shell /usr/sbin/nologin \
        trailone \
    && mkdir -p /data \
    && chown trailone:trailone /data

USER 10001:10001
WORKDIR /data

# The script writes the generated GPX beside the input GPX file. Therefore the
# directory containing the input file must be writable by the container user.
VOLUME ["/data"]

ENTRYPOINT ["python", "/app/gpx_wpt_tag_revgeo.py"]
CMD ["--help"]
