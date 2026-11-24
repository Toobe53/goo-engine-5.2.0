#!/bin/sh
# SPDX-FileCopyrightText: 2026 Goo Engine Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Sign the release checksum file with a detached GPG signature.
#
# The Linux archive itself is not code-signed.  Consumers verify the archive
# hash from SHA256SUMS and verify SHA256SUMS.asc against a published key.
#
# Usage:
#   tools/release/sign_linux.sh dist/goo-engine-5.2.1
#   GOO_GPG_KEY=0123456789ABCDEF tools/release/sign_linux.sh \
#       dist/goo-engine-5.2.1 --execute
#
# Exit codes: 0 = OK/dry-run, 1 = signing or verification failure, 2 = usage.

set -eu

usage() {
    cat <<'EOF'
usage: sign_linux.sh BUNDLE_DIR [--key KEYID] [--execute] [--output FILE]

BUNDLE_DIR must contain SHA256SUMS.  The default is a dry-run that only prints
the command.  Pass --execute to create SHA256SUMS.asc and verify it with gpg.

Environment:
  GOO_GPG_KEY   default key id/email (overridden by --key)
EOF
}

if [ "$#" -lt 1 ]; then
    usage >&2
    exit 2
fi

BUNDLE=$1
shift

KEY=${GOO_GPG_KEY:-}
EXECUTE=0
OUTPUT=

while [ "$#" -gt 0 ]; do
    case "$1" in
        --key)
            [ "$#" -ge 2 ] || { usage >&2; exit 2; }
            KEY=$2
            shift 2
            ;;
        --execute)
            EXECUTE=1
            shift
            ;;
        --output)
            [ "$#" -ge 2 ] || { usage >&2; exit 2; }
            OUTPUT=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "sign_linux.sh: unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ ! -d "$BUNDLE" ]; then
    echo "sign_linux.sh: bundle directory not found: $BUNDLE" >&2
    exit 2
fi
if [ ! -f "$BUNDLE/SHA256SUMS" ]; then
    echo "sign_linux.sh: missing $BUNDLE/SHA256SUMS" >&2
    exit 2
fi
if [ -z "$OUTPUT" ]; then
    OUTPUT="$BUNDLE/SHA256SUMS.asc"
fi
if ! command -v gpg >/dev/null 2>&1; then
    echo "sign_linux.sh: gpg not found" >&2
    exit 2
fi

if [ "$EXECUTE" -eq 0 ]; then
    echo "sign_linux.sh: dry-run (pass --execute to sign)"
    echo "  gpg --batch --yes --armor --detach-sign --local-user <key> --output $OUTPUT $BUNDLE/SHA256SUMS"
    echo "  gpg --verify $OUTPUT $BUNDLE/SHA256SUMS"
    echo "sign_linux.sh: no signature written"
    exit 0
fi

if [ -z "$KEY" ]; then
    echo "sign_linux.sh: --execute needs --key or GOO_GPG_KEY" >&2
    exit 2
fi

echo "sign_linux.sh: signing with key $KEY"
gpg --batch --yes --armor --detach-sign --local-user "$KEY" --output "$OUTPUT" "$BUNDLE/SHA256SUMS"
gpg --batch --verify "$OUTPUT" "$BUNDLE/SHA256SUMS"
echo "sign_linux.sh: verified $OUTPUT"
echo "sign_linux.sh: sha256 $(sha256sum "$OUTPUT" | awk '{print $1}')"
