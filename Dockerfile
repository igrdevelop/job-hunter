FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    libreoffice \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock
RUN playwright install chromium --with-deps

COPY . .
RUN pip install --no-cache-dir -e . --no-deps

RUN mkdir -p Applications backups

CMD ["python", "-m", "hunter"]