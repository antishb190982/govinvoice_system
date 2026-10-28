FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 GOVBILLS_ENV=production \
    PRIVATE_STORAGE_ROOT=/data/private SIGNING_CA_DIR=/data/ca

RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 govbills \
    && mkdir -p /data/private /data/ca && chown -R govbills /data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=govbills . .
# collectstatic needs settings to import; throwaway values are used for that step only.
RUN DJANGO_SECRET_KEY=build FIELD_ENCRYPTION_KEYS=dKmXGzVjHGsW2yQ1vO0kAbAFxdWf2Vb0r2yK8-8fWyU= \
    SIGNING_CA_PASSPHRASE=build python manage.py collectstatic --noinput \
    && chown -R govbills /app/var

USER govbills
VOLUME ["/data"]
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", \
     "--access-logfile", "-", "--forwarded-allow-ips", "*", "--limit-request-line", "8190"]
