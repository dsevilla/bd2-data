FROM python:3-slim

WORKDIR /

COPY preprocess.sh /
COPY URL /
COPY *.py /

RUN apt-get -qq update \
    && apt-get install -y --no-install-recommends curl parallel p7zip-full \
    && rm -rf /var/lib/apt/lists/*

CMD [ "sh", "/preprocess.sh" ]
