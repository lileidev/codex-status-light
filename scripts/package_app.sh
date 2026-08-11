#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
configuration=${1:-release}
destination=${2:-"$HOME/Applications/AgentsLight.app"}

swift build --package-path "$project_root" -c "$configuration"
binary_path=$(swift build --package-path "$project_root" -c "$configuration" --show-bin-path)/AgentsLight

mkdir -p "$destination/Contents/MacOS"
mkdir -p "$destination/Contents/Resources"
cp "$binary_path" "$destination/Contents/MacOS/AgentsLight"
cp "$project_root/app/Info.plist" "$destination/Contents/Info.plist"

ENTITLEMENTS="$project_root/app/AgentsLight.entitlements"
SIGNING_IDENTITY="${SIGNING_IDENTITY:--}"

codesign \
    --sign "$SIGNING_IDENTITY" \
    --force \
    --options runtime \
    --entitlements "$ENTITLEMENTS" \
    --timestamp=none \
    "$destination"

printf '%s\n' "$destination"
