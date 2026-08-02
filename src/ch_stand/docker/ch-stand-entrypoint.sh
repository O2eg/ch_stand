#!/bin/sh
set -eu

SSH_SOURCE_ROOT=/ch-stand-ssh-source

if [ ! -f "$SSH_SOURCE_ROOT/ch_stand_test.pub" ]; then
  echo "missing generated ch-stand SSH public key" >&2
  exit 78
fi

install -d -m 0700 -o root -g root /root/.ssh
install -m 0600 -o root -g root \
  "$SSH_SOURCE_ROOT/ch_stand_test.pub" /root/.ssh/authorized_keys

ssh-keygen -A
/usr/sbin/sshd

if [ "${CH_STAND_ROLE:-server}" = "keeper" ]; then
  exec clickhouse keeper --config-file=/etc/clickhouse-keeper/keeper_config.xml
fi

exec /entrypoint.sh "$@"
