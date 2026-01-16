ARG BUILD_FROM
FROM ${BUILD_FROM}

# Avoid pip/PEP668 hassles: use Alpine packages
RUN apk add --no-cache \
    python3 \
    py3-evdev \
    py3-requests

COPY run.py /run.py

CMD ["python3", "/run.py"]
