# Chromium needs a real X display to run headed, which is also what lets you
# log in by hand over noVNC. So the container runs four processes under
# supervisor: Xvfb, x11vnc, websockify/noVNC, and the app itself.
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DISPLAY=:99 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        x11vnc \
        novnc \
        websockify \
        supervisor \
        ca-certificates \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Requirements first so edits to the source do not invalidate the heavy layers.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --with-deps pulls the shared libraries Chromium needs; this is the slow layer.
RUN playwright install --with-deps chromium

COPY app/ /app/app/
COPY config/ /app/config/
COPY docker/ /app/docker/

RUN chmod +x /app/docker/entrypoint.sh

# Chromium's sandbox needs privileges we would rather not grant the container,
# so it runs as a normal user with --no-sandbox instead of as root.
RUN useradd --create-home --uid 1000 npd \
    && mkdir -p /state \
    && chown -R npd:npd /state /app \
    # Xvfb cannot create this itself once we drop to a non-root user.
    && mkdir -p /tmp/.X11-unix \
    && chmod 1777 /tmp/.X11-unix
USER npd

EXPOSE 8765 6080

ENTRYPOINT ["/app/docker/entrypoint.sh"]
