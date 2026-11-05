#!/usr/bin/env bash
# Write the BUILD#<env> / BUILD record the site panel shows on its Docker and Pipeline cards.
# Called from platform-ci.yml after the image has passed Trivy and cosign.
#
#   put-build-record.sh <env> <image@sha256:...> <trivy.json> [promoted_from]
#
# Needs: AWS credentials for the env's build-writer role, TABLE, docker buildx, jq.
set -euo pipefail

env=$1 ref=$2 trivy_json=$3 promoted_from=${4:-}
digest=${ref#*@}

# Revision and build time come from the image's own OCI labels, so prod reports the
# commit that built the image on staging, not the merge commit on main.
labels=$(docker buildx imagetools inspect "$ref" --format '{{ json .Image }}' |
  jq '."linux/arm64".config.Labels // .config.Labels // {}')
revision=$(jq -r '."org.opencontainers.image.revision" // "unknown"' <<<"$labels")
created=$(jq -r '."org.opencontainers.image.created" // "unknown"' <<<"$labels")

# Compressed size of the arm64 image (what the home cluster pulls).
arm64_manifest=$(docker buildx imagetools inspect --raw "$ref" |
  jq -r '.manifests[] | select(.platform.os == "linux" and .platform.architecture == "arm64") | .digest')
size=$(docker buildx imagetools inspect --raw "${ref%@*}@$arm64_manifest" | jq '[.layers[].size] | add')

# Fixable findings by severity (the scan runs with --ignore-unfixed).
counts=$(jq '[.Results[]?.Vulnerabilities[]?.Severity] as $s
  | reduce ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN") as $k ({}; .[$k] = ([$s[] | select(. == $k)] | length))' \
  "$trivy_json")

item=$(jq -n \
  --arg env "$env" --arg digest "$digest" --arg revision "$revision" --arg created "$created" \
  --arg size "$size" --argjson counts "$counts" --arg promoted "$promoted_from" \
  --arg run "${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-}" \
  --arg recorded "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '
  {
    pk: {S: "BUILD#\($env)"},
    sk: {S: "BUILD"},
    commit: {S: $revision},
    image_digest: {S: $digest},
    build_time: {S: $created},
    image_size_bytes: {N: $size},
    signed: {BOOL: true},
    trivy: {M: ($counts | map_values({N: tostring}))},
    run_url: {S: $run},
    recorded_at: {S: $recorded}
  } + (if $promoted == "" then {} else {promoted_from: {S: $promoted}} end)')

aws dynamodb put-item --table-name "${TABLE:?}" --item "$item"
jq -c '{env: "'"$env"'", commit: .commit.S, digest: .image_digest.S, size: .image_size_bytes.N, trivy: (.trivy.M | map_values(.N))}' <<<"$item"
