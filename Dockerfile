FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml README.md main.py topics.json journals.json ./
COPY paper_trend/ ./paper_trend/
RUN python -m pip install --no-cache-dir --no-deps -e .

# Keep the workspace available for repeated docker exec sessions.
CMD ["sleep", "infinity"]
