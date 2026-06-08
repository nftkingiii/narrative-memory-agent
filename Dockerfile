FROM python:3.12-slim

WORKDIR /app

# Install Node.js
RUN apt-get update && apt-get install -y curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install npm packages
RUN npm install -g bitget-hub @bitget-ai/getagent-skill

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project
COPY . .

# Start dashboard first (so Railway health check passes), then agent
CMD python3 -m uvicorn dashboard.app:app --host 0.0.0.0 --port $PORT & python3 main.py & wait