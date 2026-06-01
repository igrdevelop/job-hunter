FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    libreoffice \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium --with-deps

# Install the package (non-editable — no live-edit needed in Docker).
COPY hunter/ hunter/
RUN pip install --no-cache-dir . --no-deps

COPY . .

RUN mkdir -p Applications backups

CMD ["python", "-m", "hunter"]