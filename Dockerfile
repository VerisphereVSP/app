FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy core artifacts (for ABI loading — includes VeriSphereForwarder)
COPY core/out /core/out

# Create and activate virtual environment
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

# Copy and install requirements
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Explicit fallback installs
RUN pip install --no-cache-dir "sqlalchemy>=2.0.52,<2.1" python-dotenv psycopg2-binary  # hotfix_sqlalchemy21

# Verify key packages are installed
RUN pip list | grep -E "sqlalchemy|python-dotenv|psycopg2" || exit 1

# Copy application code
COPY app/ /app/
COPY app/ops ./ops

EXPOSE 8070

# Use /bin/sh instead of bash (slim image doesn't have bash)
CMD ["/bin/sh", "-c", "python migrate.py && /venv/bin/uvicorn main:app --host 0.0.0.0 --port 8070"]
