# Cloud Run 2nd-gen image for the autogroupchat Signal maker.
#
# 1st gen cannot host this: the signal-cli native artifact is 110 MB gzipped
# against a 100 MB compressed source limit. Deploying an image sidesteps the
# source limit entirely and puts the binary on a real read-only filesystem
# rather than in tmpfs, where it would be charged to memory.

FROM python:3.12-slim AS fetch

# Pinned deliberately. Bumping this means re-running `doctor` and re-checking
# the JSON output shapes in spec section 7 against the new release's sources.
ARG SIGNAL_CLI_VERSION=0.14.7
ARG SIGNAL_CLI_SHA256=0fe065294adcf35df4c249b635d0ce57de7765d4fec660bffaa2e7f0549d4e5f

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /fetch
RUN ARCHIVE="signal-cli-${SIGNAL_CLI_VERSION}-Linux-native.tar.gz" \
 && curl -fsSLO "https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/${ARCHIVE}" \
 && echo "${SIGNAL_CLI_SHA256}  ${ARCHIVE}" | sha256sum -c - \
 && mkdir -p /opt/signal-cli \
 && tar -xzf "${ARCHIVE}" -C /opt/signal-cli \
 && chmod +x /opt/signal-cli/signal-cli

FROM python:3.12-slim

# The GraalVM native build needs no JRE. Do not add one.
COPY --from=fetch /opt/signal-cli/signal-cli /opt/signal-cli/signal-cli

WORKDIR /app

COPY deploy/signal/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY autogroupchat ./autogroupchat
COPY setup.py ./setup.py
COPY deploy/signal/main.py ./main.py

ENV PYTHONUNBUFFERED=1

# --signature-type=event keeps the legacy (event, context) handler signature,
# which functions-framework adapts from the CloudEvent Eventarc delivers.
CMD ["functions-framework", \
     "--target=autogroupchat_pubsub", \
     "--signature-type=event", \
     "--port=8080"]
