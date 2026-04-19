FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Step 1 from README: system packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    git gcc g++ make \
    python3-dev python3-pip \
    libxml2-dev libxslt1-dev \
    zlib1g-dev gettext curl \
    pkg-config \
    # mysqlclient native driver
    libmysqlclient-dev default-libmysqlclient-dev \
    # lupa (Python-Lua bridge in requirements.txt)
    liblua5.4-dev \
    && curl -fsSL https://deb.nodesource.com/setup_18.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# CSS build tools (exact from README)
RUN npm install -g sass postcss-cli postcss autoprefixer --silent

WORKDIR /code

# Python deps — cached layer, only rebuilds when requirements.txt changes
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt mysqlclient

# Path is relative to build context (online-judge/)
COPY .docker/dev-local/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8001

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python3", "manage.py", "runserver", "0.0.0.0:8001"]
