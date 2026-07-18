FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY faervell_npc ./faervell_npc
RUN pip install --no-cache-dir .
COPY behavior-pack ./behavior-pack
COPY data ./data
EXPOSE 8080
CMD ["python", "-m", "faervell_npc.main"]
