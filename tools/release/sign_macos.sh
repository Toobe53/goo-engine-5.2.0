#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Goo Engine Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Sign and notarize macOS release artifacts with codesign / notarytool.
#
# SAFETY: this script is a DRY-RUN by default. It only prints the commands it
# would run. Pass --execute to actually sign and notarize. Codesign identities
# and notarization credentials are read from the environment and are never
# echoed. This script never creates or imports a keychain.
#
# Required environment for --execute:
#   GOO_MAC_SIGN_IDENTITY   e.g. "Developer ID Application: Example (TEAMID)"
# Optional:
#   GOO_MAC_KEYCHAIN        keychain path passed to codesign --keychain
#   GOO_NOTARY_PROFILE      notarytool keychain profile name
#   APPLE_ID, TEAM_ID, APP_PASSWORD
#                           fallback credentials when no profile is set
#
# Usage:
#   tools/release/sign_macos.sh --artifacts dist/5.2.0
#   GOO_MAC_SIGN_IDENTITY="Developer ID Application: ..." \
#     tools/release/sign_macos.sh --artifacts dist/5.2.0 --execute
#
# Sign BEFORE tools/release/build_release.py so SHA256SUMS covers signed bytes.
# verify_release.py --check-signatures only inspects, and is not a substitute
# for signing on a machine that has the identity.

set -u
set -o pipefail

ARTIFACTS_DIR=""
EXECUTE=0

usage() {
    sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --artifacts)
            if [ "$#" -lt 2 ]; then
                echo "error: --artifacts requires a directory" >&2
                exit 2
            fi
            ARTIFACTS_DIR="$2"
            shift 2
            ;;
        --execute)
            EXECUTE=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "error: unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ -z "$ARTIFACTS_DIR" ]; then
    echo "error: --artifacts DIR is required" >&2
    exit 2
fi
if [ ! -d "$ARTIFACTS_DIR" ]; then
    echo "error: artifacts directory does not exist: $ARTIFACTS_DIR" >&2
    exit 2
fi

IDENTITY="${GOO_MAC_SIGN_IDENTITY:-}"
DISPLAY_IDENTITY="${IDENTITY:-<identity-not-set>}"
KEYCHAIN="${GOO_MAC_KEYCHAIN:-}"
NOTARY_PROFILE="${GOO_NOTARY_PROFILE:-}"

echo "sign_macos.sh"
echo "  artifacts: $ARTIFACTS_DIR"
echo "  mode: $([ "$EXECUTE" -eq 1 ] && echo 'EXECUTE (real signing)' || echo 'DRY-RUN (default)')"
if [ -n "$IDENTITY" ]; then
    echo "  identity: $IDENTITY"
else
    echo "  identity: <GOO_MAC_SIGN_IDENTITY not set>"
fi

if [ "$(uname -s)" != "Darwin" ]; then
    if [ "$EXECUTE" -eq 1 ]; then
        echo "error: code signing/notarization must run on macOS (uname -s = $(uname -s))" >&2
        exit 2
    fi
    echo "  warning: not running on macOS; dry-run only prints planned commands"
fi

if [ "$EXECUTE" -eq 1 ]; then
    if [ -z "$IDENTITY" ]; then
        echo "error: GOO_MAC_SIGN_IDENTITY is required for --execute" >&2
        exit 2
    fi
    if ! command -v codesign >/dev/null 2>&1; then
        echo "error: codesign not found" >&2
        exit 2
    fi
fi

# Collect candidate artifacts. App bundles are directories; everything else is
# a file. Zip archives cannot be code signed but can be notarized.
APP_BUNDLES=()
OTHER_FILES=()
ZIP_FILES=()

collect() {
    while IFS= read -r -d '' path; do
        APP_BUNDLES+=("$path")
    done < <(find "$ARTIFACTS_DIR" -type d -name '*.app' -print0 2>/dev/null)
    while IFS= read -r -d '' path; do
        OTHER_FILES+=("$path")
    done < <(find "$ARTIFACTS_DIR" -type f \
        \( -name '*.dmg' -o -name '*.pkg' -o -name '*.dylib' -o -name '*.so' \) \
        -print0 2>/dev/null)
    # Zip archives cannot be code signed but can be notarized in place.
    while IFS= read -r -d '' path; do
        ZIP_FILES+=("$path")
    done < <(find "$ARTIFACTS_DIR" -type f -name '*.zip' -print0 2>/dev/null)
}

collect

TOTAL=$(( ${#APP_BUNDLES[@]} + ${#OTHER_FILES[@]} + ${#ZIP_FILES[@]} ))
echo "  candidates: $TOTAL ($(( ${#APP_BUNDLES[@]} )) app bundle(s), $(( ${#OTHER_FILES[@]} )) signable file(s), $(( ${#ZIP_FILES[@]} )) zip file(s))"
if [ "$TOTAL" -eq 0 ]; then
    echo "  warning: no .app/.dmg/.pkg/.dylib/.so/.zip artifacts found below $ARTIFACTS_DIR"
    exit 0
fi

CODESIGN_ARGS=(--force --timestamp --sign "$DISPLAY_IDENTITY")
if [ -n "$KEYCHAIN" ]; then
    CODESIGN_ARGS+=(--keychain "$KEYCHAIN")
fi

FAILURES=0

sign_app() {
    app="$1"
    if [ "$EXECUTE" -eq 0 ]; then
        echo "  [dry-run] codesign ${CODESIGN_ARGS[*]} --options runtime \"$app\""
        echo "  [dry-run] codesign --verify --deep --strict \"$app\""
        return 0
    fi
    echo "  signing app: $app"
    if codesign "${CODESIGN_ARGS[@]}" --options runtime --deep "$app" && \
       codesign --verify --deep --strict --verbose=2 "$app"; then
        echo "  verified: $app"
        return 0
    fi
    echo "  error: failed to sign/verify $app" >&2
    return 1
}

sign_file() {
    file="$1"
    if [ "$EXECUTE" -eq 0 ]; then
        echo "  [dry-run] codesign ${CODESIGN_ARGS[*]} \"$file\""
        echo "  [dry-run] codesign --verify --strict \"$file\""
        return 0
    fi
    echo "  signing file: $file"
    if codesign "${CODESIGN_ARGS[@]}" "$file" && codesign --verify --strict --verbose=2 "$file"; then
        echo "  verified: $file"
        return 0
    fi
    echo "  error: failed to sign/verify $file" >&2
    return 1
}

for app in "${APP_BUNDLES[@]+"${APP_BUNDLES[@]}"}"; do
    sign_app "$app" || FAILURES=$(( FAILURES + 1 ))
done

for file in "${OTHER_FILES[@]+"${OTHER_FILES[@]}"}"; do
    case "$file" in
        *.dmg|*.pkg)
            # Container formats are signed directly; dylibs/so use the same call.
            sign_file "$file" || FAILURES=$(( FAILURES + 1 ))
            ;;
        *)
            sign_file "$file" || FAILURES=$(( FAILURES + 1 ))
            ;;
    esac
done

if [ "$EXECUTE" -eq 0 ]; then
    echo "dry-run: no signing performed. Re-run with --execute to sign for real."
    echo "remember: sign before build_release.py so SHA256SUMS covers signed bytes."
    exit 0
fi

# ---------------------------------------------------------------------------
# Notarization (optional). notarytool accepts dmg/pkg/zip; staple only
# dmg/pkg. A missing profile and missing credentials skip notarization with a
# warning instead of failing, because notarization is release-policy work that
# may be performed by a separate pipeline with its own secrets.
# ---------------------------------------------------------------------------
NOTARY_CREDENTIALS=()
if [ -n "$NOTARY_PROFILE" ]; then
    NOTARY_CREDENTIALS=(--keychain-profile "$NOTARY_PROFILE")
elif [ -n "${APPLE_ID:-}" ] && [ -n "${TEAM_ID:-}" ] && [ -n "${APP_PASSWORD:-}" ]; then
    NOTARY_CREDENTIALS=(--apple-id "$APPLE_ID" --team-id "$TEAM_ID" --password "$APP_PASSWORD")
fi

if [ "${#NOTARY_CREDENTIALS[@]}" -eq 0 ]; then
    echo "  warning: no notarization credentials (GOO_NOTARY_PROFILE or APPLE_ID/TEAM_ID/APP_PASSWORD); skipping notarization"
else
    if ! command -v xcrun >/dev/null 2>&1; then
        echo "  warning: xcrun not found; skipping notarization"
    else
        for file in "${OTHER_FILES[@]+"${OTHER_FILES[@]}"}" "${ZIP_FILES[@]+"${ZIP_FILES[@]}"}"; do
            case "$file" in
                *.dmg|*.pkg|*.zip)
                    echo "  notarizing: $file"
                    if ! xcrun notarytool submit "$file" "${NOTARY_CREDENTIALS[@]}" --wait; then
                        echo "  error: notarization failed for $file" >&2
                        FAILURES=$(( FAILURES + 1 ))
                        continue
                    fi
                    case "$file" in
                        *.dmg|*.pkg)
                            xcrun stapler staple "$file" || {
                                echo "  error: stapling failed for $file" >&2
                                FAILURES=$(( FAILURES + 1 ))
                            }
                            ;;
                    esac
                    ;;
            esac
        done
        # Zip archives are notarized in place but cannot be stapled.
    fi
fi

if [ "$FAILURES" -gt 0 ]; then
    echo "error: $FAILURES artifact(s) failed signing/verification/notarization" >&2
    exit 1
fi

echo "processed $TOTAL artifact(s)."
exit 0
