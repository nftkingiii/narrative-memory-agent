FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

ENV NPM_CONFIG_PREFIX=/usr/local
RUN npm install -g bitget-client bitget-hub @bitget-ai/getagent-skill

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x start.sh

ENTRYPOINT ["/bin/bash", "start.sh"]
