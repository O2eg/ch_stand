# ch-stand

`ch-stand` creates reproducible local ClickHouse test stands from strict declarative YAML.
ClickHouse servers, ClickHouse Keeper, the isolated network, host storage, generated
configuration, credentials, and cleanup are managed through the Docker SDK for Python.

The project is intended for local development, debugging, DBA training, query and schema
experiments, performance investigations, and repeatable integration tests. It is not a production
orchestrator.

## Included topologies

The bundled profiles count ClickHouse Server instances. Replicated profiles additionally run a
three-node ClickHouse Keeper quorum.

| Profile | Servers | Layout | Primary use |
|---|---:|---:|---|
| `single.yaml` | 1 | 1 shard × 1 replica | Query/schema development, profiling, version checks |
| `replica-pair.yaml` | 2 | 1 shard × 2 replicas | Replication, failover and consistency training |
| `sharded-replicated-4.yaml` | 4 | 2 shards × 2 replicas | Small production-shaped cluster, Distributed tables |
| `sharded-replicated-8.yaml` | 8 | 4 shards × 2 replicas | Fan-out, balancing, skew and larger-cluster debugging |

The four-node `2×2` topology is the most useful general cluster profile: every shard is redundant,
queries can fan out through a `Distributed` table, and the stand is still small enough for a
developer workstation. The eight-node `4×2` profile preserves two replicas per shard while making
distribution, query fan-out, uneven shard loading, and network effects easier to observe.

```text
1x1 single

  host :18123/:19000/:12220
             |
  +----------v-----------+
  | ClickHouse s01r01    |
  | HTTP/native/SSH      |
  +----------------------+

2x2 sharded and replicated

                     Distributed query/insert
                              |
                  +-----------+-----------+
                  |                       |
             shard 01                 shard 02
       +----------+----------+   +--------+----------+
       |                     |   |                   |
  +----v-----+          +----v-----+           +-----v----+          +----------+
  | s01r01   |<-------->| s01r02   |           | s02r01   |<-------->| s02r02   |
  +----------+ replica  +----------+           +----------+ replica  +----------+
       |                     |                      |                     |
       +---------------------+----------+-----------+---------------------+
                                         |
                              +----------v----------+
                              | 3-node Keeper quorum|
                              +---------------------+
```

ClickHouse replication is table-level. `ch-stand` configures Keeper, macros, cluster topology,
distributed DDL, and default `ReplicatedMergeTree` paths; it does not silently convert ordinary
`MergeTree` tables into replicated tables.

## Installation

Requirements:

- Linux with Python 3.10+;
- Docker Engine 20.10.10 or newer;
- `ssh-keygen` and an OpenSSH client;
- an x86-64-v3-capable amd64 CPU or a compatible arm64 CPU for current ClickHouse images.

### Install from PyPI

After the first release, install the package in an isolated environment and initialize an editable
stand project:

```bash
python3 -m venv .venv
.venv/bin/pip install ch-stand
.venv/bin/ch-stand init --directory local-stand
cd local-stand
../.venv/bin/ch-stand -c configs/single.yaml validate
```

### Install from source

```bash
git clone https://github.com/O2eg/ch_stand.git
cd ch_stand
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'

mkdir local-stand
.venv/bin/ch-stand init --directory local-stand
cd local-stand
../.venv/bin/ch-stand -c configs/single.yaml validate
```

The distribution and command are named `ch-stand`; the import package is `ch_stand`.
The wheel contains all example profiles, the public JSON Schema, Dockerfile, and entrypoint, so
diagnostic image builds do not require a source checkout.

## Quick start

Global options precede the command:

```bash
ch-stand init
ch-stand -c configs/single.yaml validate
ch-stand -c configs/single.yaml doctor
ch-stand -c configs/single.yaml up
ch-stand -c configs/single.yaml health
ch-stand -c configs/single.yaml sql "SELECT version(), hostname()"
ch-stand -c configs/single.yaml connection
```

Stop containers while preserving them and all data:

```bash
ch-stand -c configs/single.yaml stop
ch-stand -c configs/single.yaml restart
```

Remove containers and the managed network while keeping data:

```bash
ch-stand -c configs/single.yaml down
```

Permanent data removal is explicit:

```bash
ch-stand -c configs/single.yaml down --clear-data --force
```

Only one ch-stand project can hold the active Docker lease at a time. This prevents accidental
port and resource overlap between profiles. `ch-stand active` discovers the current lease without
a configuration file.

## ClickHouse versions

The declarative version is an official Docker tag:

```yaml
spec:
  clickhouse:
    version: "25.8.28.1"
```

Branch tags such as `25.8`, full tags such as `25.8.28.1`, and moving tags such as `latest` are
accepted. A one-command override does not rewrite YAML:

```bash
ch-stand --ch-version 26.3 -c configs/single.yaml validate
ch-stand --ch-version 26.3 -c configs/single.yaml up
```

For repeatable tests, prefer a full version. Changing the version of an existing data directory is
blocked because ClickHouse data compatibility and migrations must be reviewed explicitly. Use the
old configuration to run `down`, or intentionally create a fresh cluster:

```bash
ch-stand --ch-version 26.3 -c configs/single.yaml recreate --clear-data
```

The managed diagnostic image is built on
`clickhouse/clickhouse-server:<version>`. Alpine tags are rejected because the bundled `perf`,
SSH, and Ubuntu diagnostic package contract would no longer be reproducible. A custom apt-based
base can be supplied as `spec.clickhouse.image`.

## Diagnostic image

Every ClickHouse Server and Keeper container uses the same version-matched diagnostic image. It
contains:

- `perf` and `bpftrace` for CPU, scheduler, tracepoint, and probe investigations;
- `gdb`, `strace`, `lsof`, `pstack`/`gstack`-compatible debugger tooling, `procps`, and `psmisc`;
- `sysstat` (`iostat`, `pidstat`, `sar`), `iotop`, `fio`, and `stress-ng`;
- `ip`, `ss`, `ping`, `dig`, `nc`, `tcpdump`, and `ethtool`;
- `lshw`, `numactl`, `lsblk`, `findmnt`, `jq`, `curl`, and standard shell utilities;
- OpenSSH server with generated key-only root access.

When `spec.diagnostics.perf: true`, containers receive `PERFMON` and `SYS_PTRACE`, plus an
unconfined seccomp profile. This is intentionally optimized for isolated local diagnostics, not
production hardening. The host kernel can still deny counters or tracing through
`kernel.perf_event_paranoid`, LSM policy, unavailable tracepoints, or virtualization restrictions.
`doctor` reports the visible host policy; a successful `perf version` only proves that the tool is
installed.

Connect to the first node:

```bash
ch-stand -c configs/single.yaml ssh

# Inside the container:
perf version
perf stat -p "$(pidof clickhouse-server)" sleep 10
pidstat -p "$(pidof clickhouse-server)" 1
strace -f -p "$(pidof clickhouse-server)"
lsof -p "$(pidof clickhouse-server)"
```

For a specific generated node:

```bash
ch-stand -c configs/sharded-replicated-4.yaml ssh --node node3
ch-stand -c configs/sharded-replicated-4.yaml sql \
  --node ch-stand-2s2r-s02r01 \
  "SELECT hostname(), version()"
```

## Cluster exercises

Start the balanced four-node cluster:

```bash
ch-stand -c configs/sharded-replicated-4.yaml up
ch-stand -c configs/sharded-replicated-4.yaml cluster status
ch-stand -c configs/sharded-replicated-4.yaml keeper status
```

The generated configuration defines `ch_stand_2s2r`, unique `shard` and `replica` macros,
`internal_replication=true`, three Keeper endpoints, and these defaults:

```xml
<default_replica_path>/clickhouse/tables/{shard}/{database}/{table}</default_replica_path>
<default_replica_name>{replica}</default_replica_name>
```

Create replicated local storage on all nodes and a distributed table over it:

```bash
ch-stand -c configs/sharded-replicated-4.yaml sql "
CREATE DATABASE IF NOT EXISTS lab ON CLUSTER ch_stand_2s2r;

CREATE TABLE IF NOT EXISTS lab.events_local ON CLUSTER ch_stand_2s2r
(
    event_time DateTime,
    user_id UInt64,
    event LowCardinality(String)
)
ENGINE = ReplicatedMergeTree
PARTITION BY toYYYYMM(event_time)
ORDER BY (event_time, user_id);

CREATE TABLE IF NOT EXISTS lab.events ON CLUSTER ch_stand_2s2r
AS lab.events_local
ENGINE = Distributed(ch_stand_2s2r, lab, events_local, cityHash64(user_id));
"
```

Insert through the Distributed table and inspect placement/replication:

```bash
ch-stand -c configs/sharded-replicated-4.yaml sql "
INSERT INTO lab.events VALUES
  (now(), 1, 'open'),
  (now(), 2, 'click'),
  (now(), 3, 'close');
SYSTEM FLUSH DISTRIBUTED lab.events;
SELECT hostName(), count() FROM clusterAllReplicas(ch_stand_2s2r, lab.events_local)
GROUP BY hostName() ORDER BY hostName();
"
```

This shape is useful for teaching the distinction between:

- shards, which divide data and distributed query work;
- replicas, which copy one shard and add availability/read capacity;
- Keeper, which coordinates replicated table metadata and distributed DDL;
- local `ReplicatedMergeTree` tables and routing `Distributed` tables.

## Credentials and network boundary

No password or private key is accepted in YAML. On the first storage initialization or `up`,
ch-stand atomically creates:

```text
.ch_stand/credentials/
├── clickhouse.json          # ClickHouse username/password, mode 0600
└── ssh/
    ├── ch_stand_test        # Ed25519 private key, mode 0600
    └── ch_stand_test.pub
```

The password is stored as a SHA-256 hash in the generated ClickHouse users fragment. Cluster
connections obtain the plaintext password from a container environment substitution; normal CLI,
machine output, status, show, and applied-state files redact it. `connection --show-password` is an
explicit human-only escape hatch and is rejected in machine mode.

All HTTP, native, SSH, and Keeper host ports bind to `127.0.0.1` in bundled profiles. The default
ClickHouse user is reachable from the isolated Docker network and loopback only. These are local
test credentials and must never be reused in production.

## Host storage

ClickHouse data and logs use bind mounts under the declared relative storage root:

```text
.ch_stand/<stand>/
├── .ch-stand-applied.json
├── <stand>-s01r01/
│   ├── config/
│   ├── data/
│   └── log/
├── <stand>-s01r02/
│   └── ...
└── <stand>-keeper-01/
    ├── config/
    ├── data/
    └── log/
```

The resolved storage root must be a strict descendant of the current project directory. Symlink
escapes are rejected for the root, node directories, generated configuration, credentials, and
project assets.

```bash
ch-stand -c CONFIG storage init
ch-stand -c CONFIG storage status
ch-stand -c CONFIG storage clean --force
```

Storage cleanup refuses to run while managed containers exist. Root-owned files created by
ClickHouse are removed through a narrowly mounted, labeled helper based on the already-built
diagnostic image; the helper never receives a broader host path.

## Declarative format

The API is `ch_stand/v1`, kind `ClickHouseStand`. Unknown fields are errors. The public schema is
`schema/ch_stand-v1.schema.json`.

```yaml
api_version: ch_stand/v1
kind: ClickHouseStand

metadata:
  name: ch-stand-2s2r

spec:
  clickhouse:
    version: "25.8.28.1"
    cluster_name: ch_stand_2s2r
    database: default
    user: default
    settings:
      max_threads: 4
      log_queries: true

  topology:
    shards: 2
    replicas: 2
    keeper_nodes: 3

  docker:
    pull_policy: missing

  storage:
    root_directory: .ch_stand/ch-stand-2s2r

  ports:
    bind_address: 127.0.0.1
    http_base: 18140
    native_base: 19020
    ssh_base: 12240
    keeper_base: 19181

  resources:
    server: {cpu_limit: 1.0, memory_limit: 2g, shm_size: 256m}
    keeper: {cpu_limit: 0.5, memory_limit: 512m, shm_size: 128m}

  diagnostics:
    perf: true
```

Node names and ports are deterministic. Nodes are ordered by shard, then replica; each service
port is its declared base plus the zero-based node index. Multi-node topologies require three
Keeper nodes. The initial schema supports up to 16 shards, four replicas per shard, and 32 total
ClickHouse Server nodes.

`spec.clickhouse.settings` contains default profile settings, not arbitrary server XML. ch-stand
owns ports, networking, macros, Keeper endpoints, distributed DDL, replication defaults, logging
paths, the password, and access-management fields.

## Planning and lifecycle safety

`up` is idempotent only when desired YAML matches the applied state. Resource ownership requires
all of the following:

- the `io.ch-stand.managed=true` label;
- matching project and project-directory instance labels;
- the expected resource kind;
- the managed name suffix;
- the current configuration hash when a resource is reused.

Review changes before applying them:

```bash
ch-stand -c CONFIG plan
ch-stand -c CONFIG apply --restart --plan-hash sha256:...
```

Ports, resource limits, diagnostic flags, labels, and ClickHouse profile settings require a
container restart. Version, base image, cluster identity, topology, network identity, or storage
identity changes are blocked because they can reinterpret existing data. A reviewed plan hash
prevents applying a different configuration than the one an operator inspected.

Available lifecycle and observation commands:

```text
active
init, validate, show
plan, apply --restart
up, status, health, stop, restart, down, recreate --clear-data
cluster status, keeper status
sql, logs, connection, ssh
doctor, capabilities
image status, image build
storage init, storage status, storage clean
cleanup status, cleanup run
```

`cleanup run` requires at least one explicit scope and `--force`. `--all` selects managed
containers, storage, credentials, and the exact owned diagnostic image. Docker-global build cache,
unlabeled resources, foreign images, and paths outside the project are out of scope.

## Machine interface

The human lifecycle remains the primary interface. Automation can use the same versioned component
transport already used by the companion PostgreSQL and ClickHouse diagnostic tools:

```bash
ch-stand --machine --request-id stand-001 --component-capabilities
ch-stand --machine --request-id stand-002 -c stand.yaml validate
ch-stand --machine --request-id stand-003 -c stand.yaml plan
```

The envelope is `pg_play/component/v1`, capabilities are `pg_play/capabilities/v1`, and the stand
artifact schema remains ClickHouse-specific `ch_stand/v1`. Arbitrary SQL, interactive SSH, and
password output are unavailable in machine mode.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest

# Real Docker smoke tests:
CH_STAND_KEEP_IMAGES=1 sh tests/integration_smoke.sh single.yaml ch_stand_single
sh tests/integration_smoke.sh replica-pair.yaml ch_stand_replica_pair
```

Build and inspect release artifacts:

```bash
.venv/bin/python -m build
.venv/bin/python -m twine check dist/*
```

A tag matching the package version, for example `v0.1.0`, runs unit and Docker integration tests,
builds the wheel and source distribution, smoke-tests the installed wheel, and publishes through
PyPI Trusted Publishing. Before the first release, configure the GitHub `pypi` environment and a
PyPI trusted publisher for repository `O2eg/ch_stand` and workflow
`.github/workflows/publish.yml`.

## Scope and limitations

- The initial contract targets local self-managed ClickHouse clusters on Docker Engine.
- TLS is not part of `ch_stand/v1`; published ports are loopback-only and authentication is still
  mandatory. SSH is key-only.
- Automatic failover, traffic proxies, backups, upgrades, shard rebalancing, Kubernetes, object
  storage, and production Keeper operations are outside the current scope.
- `perf` and BPF behavior depends on the host kernel and container runtime policy.
- Moving tags such as `latest` are accepted for exploration but are not reproducible.

## References

- [Official ClickHouse Docker image](https://clickhouse.com/docs/get-started/setup/self-managed/docker)
- [Official deployment and scaling example](https://clickhouse.com/docs/guides/oss/deployment-and-scaling/examples/2-shards-1-replica)
- [ReplicatedMergeTree engines](https://clickhouse.com/docs/reference/engines/table-engines/mergetree-family/replication)
- [ClickHouse Keeper](https://clickhouse.com/docs/guides/sre/keeper/clickhouse-keeper)

## License

`ch-stand` is distributed under the
[MIT License](https://github.com/O2eg/ch_stand/blob/main/LICENSE). The license file is included in
both wheel and source distributions.
