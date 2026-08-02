# Architecture and ownership contract

`ch-stand` has a declarative control plane and a deliberately narrow ownership boundary.

```text
YAML -> strict config model -> generated XML/credentials -> Docker SDK
                                                        |-> diagnostic image
                                                        |-> server containers
                                                        |-> Keeper containers
                                                        `-> labeled network

project root
`-> .ch_stand/
    |-> credentials/          generated once per project
    `-> <stand>/              config, data, logs, applied state
```

## Module boundaries

- `config.py` validates `ch_stand/v1`, expands environment placeholders, and derives deterministic
  node/Keeper identities and ports.
- `render.py` creates ClickHouse server, user, and Keeper XML fragments without persisting plaintext
  passwords.
- `credentials.py` atomically generates and validates the ClickHouse password and Ed25519 SSH key.
- `assets.py` exposes installed-wheel Docker assets and safely initializes editable project files.
- `runtime.py` owns images, leases, networks, containers, readiness, state, storage, diagnostics,
  SQL, SSH, and scoped cleanup through the Docker SDK.
- `cli_parser.py` defines the human command surface; `cli.py` executes it and wraps supported
  commands in the machine envelope from `orchestration.py`.

## Topology model

The topology is the Cartesian product of shards and replicas. Node ordering is stable:

```text
for shard in 1..shards:
  for replica in 1..replicas:
    node_index += 1
```

This ordering defines generated hostnames and published ports. The standard profiles keep two
replicas per shard as clusters scale from two to four to eight ClickHouse Server nodes. All
multi-node profiles use three dedicated Keeper containers to make quorum behavior observable and
avoid teaching a two-member coordination design with no failure tolerance.

## Secrets

YAML and public state never contain passwords. The generated user fragment contains only a
SHA-256 password hash. `remote_servers` uses ClickHouse `from_env` substitution so the same
credential can authenticate distributed queries without embedding plaintext in generated XML.
The environment is not returned by status/capability commands. The operator can request the
password only through the human `connection --show-password` command.

## Diagnostic privilege

`diagnostics.perf=true` is an explicit local-lab tradeoff. It adds `PERFMON`, `SYS_PTRACE`, and an
unconfined seccomp profile to each server and Keeper container. It does not add `SYS_ADMIN`, mount
host devices, use privileged mode, or bypass the project storage boundary. Host kernel sysctls and
LSM rules remain authoritative.

## Destructive operations

Destructive operations require explicit CLI flags. Resource discovery is label- and suffix-scoped.
Storage roots, generated child paths, project assets, and credentials reject symlinks. ch-stand
never prunes daemon-global Docker cache and never removes an image without its managed labels and
tag suffix.
