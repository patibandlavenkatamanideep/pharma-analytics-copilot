#!/bin/bash
# Fetch the local observability drill's tools: the official OpenTelemetry
# Collector (contrib) and Prometheus release builds, pinned by version and by
# the SHA-256 their projects publish, verified before anything is unpacked.
#
#   scripts/fetch_ops_tools.sh <dir>
#
# Only darwin-arm64 is pinned (the machine the drill was run on). Another
# platform needs its own pinned digests, from the same release pages.
set -euo pipefail
dir=${1:?usage: fetch_ops_tools.sh <dir>}
mkdir -p "$dir"
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64) ;;
  *) echo "no pinned build for $(uname -s)-$(uname -m)" >&2; exit 2;;
esac
fetch() {  # url sha256 out
  local url=$1 sum=$2 out=$3
  if [ ! -f "$out" ] || ! echo "$sum  $out" | shasum -a 256 -c - >/dev/null 2>&1; then
    curl -fsSL -o "$out.part" "$url"
    echo "$sum  $out.part" | shasum -a 256 -c - >/dev/null || { echo "checksum mismatch: $url" >&2; rm -f "$out.part"; exit 3; }
    mv "$out.part" "$out"
  fi
  echo "verified $(basename "$out") $sum"
}
cd "$dir"
fetch https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v0.162.0/otelcol-contrib_0.162.0_darwin_arm64.tar.gz \
  d5e11974d2e4adac3cc001a25137aad52f6e77ec3034ba7735cac7c219a5f97a otelcol-contrib_0.162.0_darwin_arm64.tar.gz
fetch https://github.com/prometheus/prometheus/releases/download/v3.15.0/prometheus-3.15.0.darwin-arm64.tar.gz \
  920df4d17e78b3b0175af144eb318b0c74d1cf7b1d1251b326966f0e81977260 prometheus-3.15.0.darwin-arm64.tar.gz
mkdir -p otelcol prometheus
tar -xzf otelcol-contrib_0.162.0_darwin_arm64.tar.gz -C otelcol otelcol-contrib
tar -xzf prometheus-3.15.0.darwin-arm64.tar.gz -C prometheus --strip-components=1 \
  prometheus-3.15.0.darwin-arm64/prometheus prometheus-3.15.0.darwin-arm64/promtool
# Unsigned downloads are quarantined by macOS; the digests above are the check.
xattr -dr com.apple.quarantine otelcol prometheus 2>/dev/null || true
./otelcol/otelcol-contrib --version
./prometheus/prometheus --version | head -1
