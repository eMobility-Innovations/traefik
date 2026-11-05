# Fleet image overlay

`Dockerfile` produces a Linux/amd64 image from this checked-out Traefik source and immutable
builder/runtime/WebUI inputs. The pinned Node stage regenerates the complete embedded dashboard from the upstream source and lockfile. The builder must contain the Go patch level in `build-inputs.lock` and be
passed by immutable digest. It replaces only `/usr/local/bin/traefik`, the executable selected by the pinned official-library image's `/entrypoint.sh` and `traefik` command, retaining the upstream runtime
image's entrypoint and filesystem layout. `build-image.sh` is intentionally build-only: the
organization build identity publishes a candidate by digest, then uses
`emit-promotion-manifest.sh` to emit a separate `promotion-manifest.json` conforming to
`promotion-manifest.schema.json`. The fleet-side allowlist, rather than this source overlay,
names the approved organization registry/repository.

Fleet control is the sole promotion and compose owner. It accepts that manifest only after both
the focused server gate and the real TLS first-handshake canary succeeded. Until then, it retains
the stock `docker.io/traefik:v3.7` policy. No mutable production tag, placeholder digest, credential, or timer belongs in this overlay.

HTTP/3 server-side qlog (`pkg/server/qlog_tracer.go`) is off unless `H3_QLOG_DIR`, `H3_QLOG_MAX_DIR_BYTES` and `H3_QLOG_MIN_FREE_BYTES` are all set; there are no built-in defaults. It writes `<odcid>_server.sqlog` from one background worker that cannot stall a connection, and stops, logging once, at the size cap or free-space floor. Tracing then stays off until restart unless the optional `H3_QLOG_PRUNE_AGE` duration is set, which removes only finished traces older than that age when a limit trips. Header values other than `:method` and `:status` are redacted.

`reconcile.py` is the producer entry point for the existing fleet control-plane tick. It
uses a nonblocking lock, binds the official HTTPS tag advertisement to the fetched tag object and its peeled commit, and compares the newest complete stable upstream tag plus configured
build inputs, creates a private detached worktree, and verifies the checkout has the canonical organization origin and that every patch and the explicit overlay commit belongs to fetched organization `main`. It applies the reviewed Go patch commit
series, then imports the complete overlay and image fixture from the separate immutable `overlay_commit`. Conflicts, failed gates, failed image canaries and failed
pushes leave the previous promotion manifest intact. A successful push emits the registry
digest and atomically advances the input marker. There is no independent schedule.

The fixed installed invocation is `/usr/local/sbin/traefik-readiness-reconcile`, a
wrapper for this script with its default root-owned configuration and state paths.
`/etc/traefik-readiness/producer.json` must contain exactly `source_checkout`,
`patch_commits` (full reviewed Go commits), `overlay_commit` (full reviewed organization overlay commit), `build_image`, `runtime_image`, `webui_image`
(all references must exactly equal the trusted installed `build-inputs.lock`). The fixed root-owned `/usr/local/libexec/traefik-readiness-gate` receives the candidate worktree as its only argument and runs exactly `make validate` followed by `./verify.sh`; no configurable gate or skip exists. Install its source from `overlay/traefik-readiness-gate`. An unavailable or unsafe gate fails the build. Registry authentication comes from
the dedicated builder Docker configuration, never this JSON or git. The control-plane
consumer reads `/var/lib/traefik-readiness/promotion-manifest.json` through its authenticated
fleet transport and owns staged deployment; the producer never changes an edge.

The producer runs against a controlled **bare** source mirror, never an arbitrary
checkout. Every source/configuration path and parent must belong to root, be free of
symlinks, and deny group/other writes. The mirror has exactly this local configuration:

```ini
[core]
    repositoryformatversion = 0
    filemode = true
    bare = true
[remote "origin"]
    url = https://github.com/eMobility-Innovations/traefik.git
    fetch = +refs/heads/main:refs/remotes/origin/main
```

Global/system Git configuration, inherited Git environment overrides, hooks, filesystem
monitoring, replacement objects and grafts are disabled. The producer supplies an explicit
automation committer identity. Candidate tags include the full input fingerprint and a fresh attempt UUID; an existing tag is refused. A retry after a post-image-push failure reruns the source gate and TLS proof under a new tag, so it never overwrites or blindly reuses the prior candidate. The digest returned by the push is read back from the registry;
its configuration digest must equal the current candidate configuration digest before promotion.
Local historical `RepoDigests` never select the promoted artifact.

Successful promotion JSON is published as a **data-only scratch OCI image** in the
same repository at `registry.fiszu.com/emobility/traefik:promotion-linux-amd64`.
This tag is discovery only; consumers resolve its immutable digest, verify its
config and layer hashes, then read its sole regular `promotion.json` member without
running the image. The producer first proves the local Docker archive has exactly
one such layer and its bytes match the verified candidate manifest. Only after the
candidate image and promotion artifact pushes have been read back successfully do
the local last-good manifest and input marker advance. No new service, credential,
or schedule is introduced. `verify_promotion_artifact.py` exercises the real Docker
archive locally and removes its fixture image; it never pushes a fixture.

Modern Docker uses an OCI index or manifest digest in `.Id`; a configuration
annotation is not assumed. The producer saves the exact Linux/amd64 platform into
a private archive, checks its sole image and platform, and hashes the raw config
bytes. Registry readback must match that configuration digest. Descriptor-bearing
engines never fall back to `.Id`; classic storage without a descriptor uses its
legacy config ID.

Artifact acceptance also pulls the **same immutable pushed digest** for Linux/amd64
and saves it without executing it. The pulled configuration identity, Linux/amd64
platform, sole layer's diff ID, canonical regular-file member and exact expected JSON
bytes must all pass the archive verifier before either last-good local file advances.
A failed readback leaves both files untouched; the next native tick can retry with a
fresh immutable candidate tag after repeating the acceptance checks.

## Dedicated builder installation

The native host module is assigned only to `esc:197`; its existing reconcile tick
delivers this repository's installer and two root-owned mode-0600 native inputs
into that CT. Inside the CT it invokes `overlay/install-builder.sh` with
`--source-commit`, `--producer-config` and `--publisher-docker-config`. The source
commit must be a complete ancestor of canonical organization `main`; the producer
input must name that same `overlay_commit` and `/opt/traefik-readiness/source.git`.
The publisher input is an existing native Docker configuration, copied opaquely
into `/var/lib/traefik-readiness/docker/config.json`; global Docker configuration
is never changed and no credential is printed or minted.

The installer creates a controlled bare mirror with exactly the policy above and
materializes protected source under `/opt/traefik-readiness/releases/<commit>`,
including `overlay/`, `verify.sh`, `REPO-CONTRACT.toml` and the vendor validation
pins at `.github/workflows/validate.yaml`. It checks the official Go archive
against `build-inputs.lock` and installs only into the dedicated versioned
`/opt/traefik-readiness/tools/go1.27.3` tree. It never replaces global Go.
The fixed root gate derives its compiler version from that protected lock and
executes the protected versioned `overlay/validate.sh <candidate>` followed by
the candidate's `./verify.sh`. Candidates must belong to the controlled mirror.

Successful installation atomically writes the root-only completion receipt
`/var/lib/traefik-readiness/builder-installed.json` with exactly `schema_version: 1`,
`source_commit` and `go_version`, after all runtime files activate, then emits
`INSTALLED`; absent native inputs or an active
producer lock emit `PENDING` with a reason and no invented defaults. The caller
retries pending work through its existing tick. Validation and compiler failures
leave the prior runtime wrappers in place. No service, timer or cadence is added.
The no-argument producer remains `/usr/local/sbin/traefik-readiness-reconcile`.
`--dest-root` is accepted only when the installer is sourced by the isolated root
fixture and only for `/root/traefik-builder-test-*/target`; the production executable
rejects destination overrides. The native caller always uses the fixed real paths.

Unprivileged parsing and policy tests run on the managed gate runner. The separate
`overlay/tests/builder_install_root_fixture.py` acceptance rail runs as root on
the existing builder using unique private fixture roots, fake source/download
boundaries and a fake dedicated SDK. It proves ownership, modes, checksum refusal,
last-good preservation, the producer lock, and actual fixed-gate compiler/bootstrap
dispatch while leaving production paths, registry state and other build jobs alone.
