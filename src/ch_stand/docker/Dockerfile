ARG CLICKHOUSE_BASE_IMAGE=clickhouse/clickhouse-server:25.8.28.1
FROM ${CLICKHOUSE_BASE_IMAGE}

ARG CLICKHOUSE_BASE_IMAGE
ARG CLICKHOUSE_VERSION=25.8.28.1

LABEL io.ch-stand.image-schema="1" \
      io.ch-stand.managed="true" \
      io.ch-stand.resource-kind="managed-image" \
      io.ch-stand.clickhouse-version="${CLICKHOUSE_VERSION}" \
      io.ch-stand.base-image="${CLICKHOUSE_BASE_IMAGE}"

USER root

COPY docker/ch-stand-entrypoint.sh /usr/local/bin/ch-stand-entrypoint

RUN set -eux; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        bpftrace \
        ca-certificates \
        curl \
        dnsutils \
        ethtool \
        fio \
        gdb \
        iproute2 \
        iputils-ping \
        iotop \
        jq \
        less \
        linux-tools-common \
        linux-tools-generic \
        lsof \
        lshw \
        netcat-openbsd \
        numactl \
        openssh-server \
        procps \
        psmisc \
        strace \
        stress-ng \
        sysstat \
        tcpdump \
        util-linux \
        vim-tiny; \
    rm -rf /var/lib/apt/lists/*; \
    install -d -m 0755 /run/sshd; \
    rm -f /etc/ssh/ssh_host_*; \
    chmod 0755 /usr/local/bin/ch-stand-entrypoint; \
    passwd -d root; \
    printf '%s\n' \
        'PasswordAuthentication no' \
        'KbdInteractiveAuthentication no' \
        'PermitRootLogin prohibit-password' \
        'PubkeyAuthentication yes' \
        > /etc/ssh/sshd_config.d/ch-stand.conf; \
    perf_path="$(find -L /usr/lib/linux-tools -type f -name perf -perm /111 | sort -V | tail -n 1)"; \
    test -n "${perf_path}"; \
    ln -sf "${perf_path}" /usr/local/bin/perf; \
    perf version; \
    clickhouse-client --version

EXPOSE 22 8123 9000 9009
ENTRYPOINT ["/usr/local/bin/ch-stand-entrypoint"]
