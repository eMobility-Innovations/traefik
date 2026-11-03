#!/usr/bin/env bash
# Runs the small regression rail.  `claude-gate-run` is intentionally outside this
# script: the runner invokes this file after provisioning from REPO-CONTRACT.toml.
set -euo pipefail
cd "$(dirname "$0")"

python3 -m unittest discover -s overlay/tests -p 'test_reconcile.py'
bash overlay/tests/test_promotion_manifest.sh
go test ./pkg/server -run 'Test(ServerStartup|ServerFirstHandshake|InitialConfiguration)' -count=1
go test ./pkg/tls -run '^TestUpdateConfigs' -count=1
go test ./cmd/traefik -run '^TestInitialRequiredProviders$' -count=1
go test ./pkg/provider/acme -run '^TestInitialStoredSnapshotPrecedesRenewal$' -count=1
