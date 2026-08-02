#!/usr/bin/env bash
set -euo pipefail

# These release facts are source-owned, not Docker build arguments. Changing the
# installed bytes therefore requires a new hermes-agent commit/revision tag.
readonly OPENCODE_VERSION="1.18.10"
readonly OPENCODE_LINUX_AMD64_SHA256="6b1113da704253fb4da12b41e4236acecb9f2b62949c945f6eeacaa15111b976"
readonly OPENCODE_LINUX_ARM64_SHA256="41ae3041e91b894e4c0dc06a73a9a2796254bf390ffb99626a43af5e2912d170"

target_arch=""
archive=""
install_dir="/usr/local/bin"
print_selection=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --targetarch) target_arch="${2-}"; shift 2 ;;
        --archive) archive="${2-}"; shift 2 ;;
        --install-dir) install_dir="${2-}"; shift 2 ;;
        --print-selection) print_selection=true; shift ;;
        *) echo "unsupported argument" >&2; exit 2 ;;
    esac
done

case "${target_arch}" in
    amd64)
        asset_arch="x64"
        expected_sha256="${OPENCODE_LINUX_AMD64_SHA256}"
        ;;
    arm64)
        asset_arch="arm64"
        expected_sha256="${OPENCODE_LINUX_ARM64_SHA256}"
        ;;
    *)
        echo "unsupported OpenCode target architecture" >&2
        exit 1
        ;;
esac

if [[ "${print_selection}" == true ]]; then
    printf '%s %s %s\n' "${OPENCODE_VERSION}" "${asset_arch}" "${expected_sha256}"
    exit 0
fi

temporary_archive=""
if [[ -z "${archive}" ]]; then
    temporary_archive="$(mktemp /tmp/opencode.XXXXXX.tar.gz)"
    archive="${temporary_archive}"
    trap 'rm -f "${temporary_archive}"' EXIT
    curl -fL \
        "https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}/opencode-linux-${asset_arch}.tar.gz" \
        -o "${archive}"
fi

printf '%s  %s\n' "${expected_sha256}" "${archive}" | sha256sum -c -
install -d -m 0755 "${install_dir}"
tar -xzf "${archive}" -C "${install_dir}" opencode
chmod 0755 "${install_dir}/opencode"
test "$("${install_dir}/opencode" --version)" = "${OPENCODE_VERSION}"
