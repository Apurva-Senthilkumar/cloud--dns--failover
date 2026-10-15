FROM python:3.12-slim
WORKDIR /app
COPY endpoint.py primary.py backup.py ./
ENV PYTHONUNBUFFERED=1 ENDPOINT=primary
USER nobody
# ENDPOINT=primary (default) or backup selects which script runs.
CMD ["sh", "-c", "python ${ENDPOINT}.py"]
