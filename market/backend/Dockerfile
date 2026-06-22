# EdgeLane MARKET backend — production container image.
#
# Serves the FastAPI app (bias/strategy API, webhook, /diag) AND the Torque
# order-builder page on one port. Config + Tradier tokens are NOT baked in:
# they are bind-mounted at runtime (see deploy/docker-compose.yml) so secrets
# never live in the image.
#
# Build context = market/backend/.  Built/run via deploy/docker-compose.yml.

FROM python:3.11-slim

# Faster, quieter pip + unbuffered logs for `docker logs`.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# curl is used by the compose healthcheck.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# Editable install: keeps the source tree in /srv so the Torque route can
# resolve ui/torque.html relative to app/ (it walks parents[2] → /srv/ui).
# Both app/ + ui/ are copied; pyproject's [tool.setuptools.packages.find]
# include=["app*"] scopes packaging to `app` so the ui/ dir doesn't trip
# setuptools' flat-layout "multiple top-level packages" guard.
COPY pyproject.toml ./
COPY app ./app
COPY ui ./ui
RUN pip install -e . \
 # The Webull SDK declares cryptography<42, but only its UNUSED RSA private-key
 # signer imports cryptography — our app-key auth path (and PyJWT's Supabase
 # JWT verification) use HMAC/stdlib + a modern cryptography. Override the SDK's
 # dead-weight pin so the auth backend ships current crypto, not a 2-yr-old one.
 # (Verified: SDK + option preview + ES256 JWT all work on cryptography>=43.)
 && pip install --upgrade 'cryptography>=43'

# DuckDB lives under ~/.edgelane (DB_PATH in the config) — persisted by a
# named volume in compose. CORS origins, tokens, market-hours gating all come
# from the mounted config file via EDGELANE_MARKET_CONFIG.
ENV EDGELANE_MARKET_CONFIG=/config/edgelane_market.config

EXPOSE 8789

# Host/port are CLI-driven (override the config's 127.0.0.1) so the tunnel
# sidecar can reach the app across the container network.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8789"]
