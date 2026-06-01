FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    libreoffice \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip setuptools>=64
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium --with-deps

# Copy only the package before full COPY so the editable-install layer is cached
# independently of source changes (tests, docs, configs).
COPY hunter/ hunter/
RUN pip install --no-cache-dir --no-build-isolation -e . --no-deps

COPY . .

RUN mkdir -p Applications backups

CMD ["python", "-m", "hunter"]