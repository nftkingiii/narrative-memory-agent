FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

RUN npm install -g bitget-hub @bitget-ai/getagent-skill

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENTRYPOINT ["/bin/bash", "-c", "python3 -m uvicorn dashboard.app:app --host 0.0.0.0 --port 8000 & python3 main.py & wait"]