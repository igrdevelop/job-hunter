FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    libreoffice \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium --with-deps

# No pip install needed — WORKDIR /app is on sys.path, python -m hunter works directly.
COPY . .

RUN mkdir -p Applications backups

CMD ["python", "-m", "hunter"]