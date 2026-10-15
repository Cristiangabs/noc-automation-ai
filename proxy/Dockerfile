FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    iputils-ping \
    curl \
    traceroute \
    && rm -rf /var/lib/apt/lists/*

COPY proxy.py .

EXPOSE 11440

CMD ["python3", "proxy.py"]
