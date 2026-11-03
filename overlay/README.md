# Fleet image overlay

`Dockerfile` produces a Linux/amd64 image from this checked-out Traefik source and immutable
builder/runtime/WebUI inputs. The pinned Node stage regenerates the complete embedded dashboard from the upstream source and lockfile. The builder must contain the Go patch level in `build-inputs.lock` and be
passed by immutable digest. It replaces only `/traefik`, retaining the upstream runtime
image's entrypoint and filesystem layout. `build-image.sh` is intentionally build-only: the
organization build identity publishes a candidate by digest, then uses
`emit-promotion-manifest.sh` to emit a separate `promotion-manifest.json` conforming to
`promotion-manifest.schema.json`. The fleet-side allowlist, rather than this source overlay,
names the approved organization registry/repository.

Fleet control is the sole promotion and compose owner. It accepts that manifest only after both
the focused server gate and the real TLS first-handshake canary succeeded. Until then, it retains
the stock `docker.io/traefik:v3.7` policy. No mutable production tag, placeholder digest, credential, or timer belongs in this overlay.

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
(all immutable references), and `gate_command` (absolute installed executable). The gate
receives the candidate worktree as its only argument and must run `make validate` and
`./verify.sh`; an unavailable gate fails the build. Registry authentication comes from
the dedicated builder Docker configuration, never this JSON or git. The control-plane
consumer reads `/var/lib/traefik-readiness/promotion-manifest.json` through its authenticated
fleet transport and owns staged deployment; the producer never changes an edge.
