FROM kbase/sdkpython:3.8.0
LABEL maintainer="ac.shahnam"
# -----------------------------------------
# In this section, you can install any system dependencies required
# to run your App.  For instance, you could place an apt-get update or
# install line here, a git checkout to download code, or run any other
# installation scripts.

# RUN apt-get update

# ---- System dependencies ---------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget \
        curl \
        ca-certificates \
        openjdk-11-jre-headless \
    && rm -rf /var/lib/apt/lists/*
RUN pip install gunicorn gevent 
# ---- Install Canu 2.2 binary -----------------------------------------------
# Canu is distributed as pre-built Linux x86_64 binaries; no compilation needed.
ENV CANU_VERSION=2.2
ENV CANU_INSTALL=/opt/canu
ENV PATH="${CANU_INSTALL}/bin:${PATH}"
 
RUN wget -q \
    "https://github.com/marbl/canu/releases/download/v${CANU_VERSION}/canu-${CANU_VERSION}.Linux-amd64.tar.xz" \
    -O /tmp/canu.tar.xz \
    && mkdir -p ${CANU_INSTALL} \
    && tar -xJf /tmp/canu.tar.xz --strip-components=1 -C ${CANU_INSTALL} \
    && rm /tmp/canu.tar.xz \
    && chmod +x ${CANU_INSTALL}/bin/canu
 
# ---- Copy module code and run version check --------------------------------
COPY ./ /kb/module
RUN mkdir -p /kb/module/work/tmp
 
WORKDIR /kb/module

RUN make all

ENTRYPOINT [ "./scripts/entrypoint.sh" ]

CMD [ ]
