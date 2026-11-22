FROM ghcr.io/home-assistant/base:3.24-2026.06.1

ARG BUILD_VERSION
ARG BUILD_ARCH

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}"

# Avoid pip/PEP668 hassles: use Alpine packages
RUN apk add --no-cache \
    python3 \
    py3-evdev \
    py3-requests

COPY run.py /run.py

CMD ["python3", "/run.py"]
