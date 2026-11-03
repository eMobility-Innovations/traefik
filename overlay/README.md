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

Docker storage engines expose different image identities: containerd-backed Docker
uses the manifest digest in `.Id`, with the configuration digest in
`.Descriptor.annotations["config.digest"]`; classic storage uses the config ID.
The producer binds registry configuration digests to that explicit local config
identity and refuses an unsupported descriptor without that annotation.

Artifact acceptance also pulls the **same immutable pushed digest** for Linux/amd64
and saves it without executing it. The pulled configuration identity, Linux/amd64
platform, sole layer's diff ID, canonical regular-file member and exact expected JSON
bytes must all pass the archive verifier before either last-good local file advances.
A failed readback leaves both files untouched; the next native tick can retry with a
fresh immutable candidate tag after repeating the acceptance checks.
