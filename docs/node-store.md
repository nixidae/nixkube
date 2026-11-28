# The node's store server and client

Status: plan, written 2026-11-27 at nixidae cc1439ed.
nixidae/nixidae#1661 holds the goal and the progress.

The node pod talks to a Nix store in two roles. It runs a **server** on a
Unix socket, and its CSI and NRI code is a **client** of that socket. Today
the server is `nix daemon` (`nix_daemon.py`) and the client is the `nix`
CLI, started as a subprocess for each operation. This page says how both
roles change:

- The server becomes a choice of three: `nix-daemon`, `pynixd` (pynixd
  with a private nix-daemon under it), and `pynixd-daemonless` (pynixd
  answers every operation and writes the store itself).
- The client becomes huggorm: Nix's own client code, in process, through
  `huggorm.AsyncStore`.

## Decisions

1. **The node keeps a server of its own, also on a host-store node.** The
   host's daemon cannot reach nixkube's substituters (an in-cluster name and
   nixkube's SSH key). A pod whose closure the host lacked did not start
   (sg1, 2026-11-19; `cli.py` `store_loops`). So a host-store node keeps its
   own socket, `NIX_DAEMON_SOCKET_PATH`, over the host's `/nix`.
2. **The daemon protocol is the only contract between client and server.**
   All three servers speak it on the same socket. The client opens
   `unix://<socket>` and does not know which server answers. A difference
   between the servers is a server defect, and the pynixd client suite
   (`pynixd/tests/client`, nixidae/nixidae#1249) is where it is found.
3. **The server choice comes first, the client port second.** The `nix` CLI
   already talks to pynixd over the socket. So the server choice can land
   and run on sg1 before the client port, and each step has its own test.
4. **The client port has no fallback to the CLI.** One module moves to
   huggorm, its tests move with it, and its CLI code goes in the same
   commit.
5. **Evaluation runs in a huggorm worker, not in the node process.**
   `flakeRef` and `nixExpr` evaluate arbitrary code. A worker
   (`huggorm.worker`) keeps the evaluator's memory and a crash out of the
   CSI and NRI server.
6. **Each ported operation is a span.** It is a child of the current
   publish span, named `store <operation>`, with its counts as attributes.
   A port that adds no span is not done. User, 2026-11-27: trace every step
   on sg1, from the build that pynixd's central receives to the container
   that runs it.

## Tracing

The chain on sg1, and where it is broken:

1. pynixd's central traces the build attempt (nixidae/nixidae#1570) and
   writes its `traceparent` into the build pod's volume attributes.
2. `NodePublishVolume` is a span under that `traceparent`
   (nixidae/nixidae#1654), and each step of the publish is a child span.
3. Each ported store operation is a child span of its step (decision 6).
4. **Broken:** the node's pynixd traces each operation it serves, but the
   call does not carry the publish's trace. The daemon protocol has a
   carrier, the `open-telemetry` feature, and pynixd reads it. huggorm's
   client does not send it yet (nixidae/nixidae#1664).
5. **Not traced:** a call that still runs the `nix` CLI, until its port
   lands; and the NRI path, which has no `traceparent` to start from.

## The server

| server | what runs | who writes `/nix` |
| --- | --- | --- |
| `nix-daemon` | `nix daemon --store local` | Nix |
| `pynixd` | pynixd, `local-socket` store, `daemon: true` | Nix, through pynixd's private daemon |
| `pynixd-daemonless` | pynixd, `local-socket` store, `daemon: false`, `pynixd_writes: true` | pynixd |

On a host-store node the host also writes `/nix`: its own nix-daemon, and on
sg1 the host's pynixd, which collects garbage. Each writer takes Nix's locks
(`daemonless.md`, "Locks Nix honours"). Two facts limit the daemonless server
there:

- **Schema migration.** `check_host_version` asks the host's daemon for its
  Nix version through huggorm (`Store::getVersion`), the one call the node
  makes to that daemon. A nix-daemon server must not be newer than the
  host's Nix, because a newer Nix migrates the database forward. A pynixd
  server asks `pynixd shares-store <version>`, which applies
  `shared_database_refusal` (`pynixd_store/create_store.py`): both Nix
  versions in `SHARED_DATABASE_NIX` (2.34 to 2.36), and on the same side of
  2.35, where the realisations table, the self-reference hash and Nix's
  migrations change. pynixd's own `Pynixd*` tables need no rule: Nix reads
  its tables by name. A pynixd with a nix-daemon under it takes both rules.
  A refusal stops the node before its server starts.
- **The big lock.** pynixd holds the global lock shared for the store's
  lifetime (nixidae/nixidae#1528). A host Nix upgrade that migrates then
  waits until the node's pynixd restarts.

## The client

Each `nix` subprocess of the node maps to one huggorm call. The order is
from the least risk to the most.

| module | today | huggorm |
| --- | --- | --- |
| `nix/system.py` | `nix eval builtins.currentSystem` | `huggorm.current_system()` |
| `store_server.py` | `nix store info` (host version) | `get_version`: done, nixidae/nixidae#1662 |
| `nix/closure.py` | `nix path-info --recursive` | `compute_fs_closure` |
| `nix/verify.py` | `nix store verify --no-trust` | `nar_from_path` hashed against `query_path_info` |
| `nix/gc.py` | `nix build --out-link` of valid paths | `add_perm_root` |
| `nix/build.py` (store paths) | `nix build --out-link` with `--extra-substituters` | `build_paths`, then `add_perm_root` |
| `cache.py` | `nix copy --to`, `nix path-info` | `copy_closure` to the cache store |
| `nix/database.py` | `nix-store --dump-db \| --load-db` | `make_validity_registration`, then `LocalStore::registerValidPaths`: **gap**, nixidae/nixidae#1663 |
| `nix/build.py` (`flakeRef`, `nixExpr`) | `nix build` | evaluation in a worker, then `build_paths_with_results` |

Out of scope: `nixos_switch.py` and `nixos_rollout.py`. They switch the host's
system profile, and run on the host's Nix.

**Substituters live in the server.** A Nix client sends its settings when it
opens a connection, and huggorm's settings are global to a process, so the
node sends none per call. The node's nix.conf names
`ssh-ng://nix@pynixd?priority=20` when the pynixd cache is on, and every
server reads it: Nix's daemon directly, pynixd through `nix config show`.
No `trusted=1`: the server's configuration serves every client, untrusted
ones too, so a path from pynixd needs a signature. The cluster's signing key
is generated at runtime (`nix-key`), so the supervisor starts the server with
`NIX_CONFIG` trusting its public half. A path pynixd substitutes keeps only
its cache's signature, so the node also trusts every key pynixd's own
nix.conf trusts (`kubenix/config.nix`); pynixd re-signs nothing. The SSH config gives `Host pynixd` a 5 s `ConnectTimeout`: an
address that drops the SYN cost 135 s per connect without one. The node
never passed `--builders`; it builds nothing itself.

**Threads.** huggorm runs every store call on one shared pool, and a call
waits there for a free thread. nixkube sizes it to `NODE_STORE_THREADS`
(32) at start and lets at most `NODE_STORE_BUILDS` (24) builds hold a
thread, so the rest stay free for short calls such as `/readyz`. Measured
with the default 4: four calls stuck on a silent socket left a live call
unanswered (nixidae/nixidae#1672).

## Open questions

- **The chroot database.** `--load-db` writes a chroot store's `db.sqlite`
  for paths that are already on disk. huggorm binds no call that registers
  without a copy. Two routes: bind `registerValidPaths` in huggorm, or call
  `pynixd_store.register`, which writes the same rows with Nix's statements.
  The binding keeps one dependency; it is the recommendation.
- **Builds on a daemonless node.** A daemonless pynixd in a pod cannot mount
  `/proc` in a sandbox (sg1, 2026-11-19). The node then needs `no_schedule`
  and a builder: the cluster's pynixd, or builder pods.
- **A read-only store.** nixidae/nixidae#1622: a daemonless pynixd must
  remount the store writable in its own mount namespace, as Nix's daemon
  does.
