#!/usr/bin/env bash
# Build Little Toby for iPhone and package it as an unsigned IPA.
#
#   ios/ci/build_ipa.sh [version]      (run from anywhere, on a Mac with Xcode)
#
# Leaves ios/LittleToby-unsigned.ipa. It's unsigned on purpose: iPhones only
# install signed apps, and the signing is done with your own Apple ID when
# you sideload it (docs/COMPANION.md). Needs `xcodegen` on the PATH.
set -euo pipefail

cd "$(dirname "$0")/.."
version="${1:-}"
extra=()
if [ -n "$version" ]; then
  extra+=("MARKETING_VERSION=$version")
fi
if [ -n "${GITHUB_RUN_NUMBER:-}" ]; then
  extra+=("CURRENT_PROJECT_VERSION=$GITHUB_RUN_NUMBER")
fi

xcodegen generate
rm -rf Payload LittleToby-unsigned.ipa
xcodebuild -project LittleToby.xcodeproj -scheme LittleToby -configuration Release \
  -sdk iphoneos -destination 'generic/platform=iOS' -derivedDataPath build/release \
  CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY="" \
  ${extra[@]+"${extra[@]}"} build | tail -n 60

mkdir -p Payload
cp -R build/release/Build/Products/Release-iphoneos/LittleToby.app Payload/
zip -qry LittleToby-unsigned.ipa Payload

echo "== what was built =="
ls -la LittleToby-unsigned.ipa
file Payload/LittleToby.app/LittleToby
du -sh Payload/LittleToby.app
plutil -p Payload/LittleToby.app/Info.plist | head -40
