#!/usr/bin/env bash
# Runs the small regression rail.  `claude-gate-run` is intentionally outside this
# script: the runner invokes this file after provisioning from REPO-CONTRACT.toml.
set -euo pipefail
GOMAXPROCS=4
GOFLAGS='-p=4'
export GOMAXPROCS GOFLAGS
cd "$(dirname "$0")"

python3 overlay/tests/check_python_coverage.py
bash overlay/tests/test_promotion_manifest.sh
bash overlay/tests/test_shell_detection.sh
go test ./pkg/server -run 'Test(ServerStartup|ServerFirstHandshake|InitialConfiguration)' -count=1
# The protected producer builds with CGO disabled, where -race cannot run.
if [ "$(go env CGO_ENABLED)" = 1 ]; then
  go test ./pkg/server -run '^TestQlog' -race -count=1
else
  go test ./pkg/server -run '^TestQlog' -count=1
fi
go test ./pkg/tls -run '^TestUpdateConfigs' -count=1
go test ./cmd/traefik -run '^TestInitialRequiredProviders$' -count=1
go test ./pkg/provider/acme -run '^TestInitialStoredSnapshotPrecedesRenewal$' -count=1
