#!/bin/sh
# Build "Meeting Transcriber.app", a Mac front end for the `transcribe` command.
#
#   macos/build.sh             build into macos/build/
#   macos/build.sh --install   build, then copy it to /Applications
#
# Needs only what ships with macOS plus the Xcode command line tools (for the icon).
# The app calls `transcribe` from ~/.local/bin, so install the CLI first.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
out="$here/build"
app="$out/Meeting Transcriber.app"
plist="$app/Contents/Info.plist"
res="$app/Contents/Resources"
pb=/usr/libexec/PlistBuddy

rm -rf "$out"
mkdir -p "$out"
osacompile -o "$app" "$here/MeetingTranscriber.applescript"

# Icon: drawn by icon.swift, then packed into the droplet's .icns.
if command -v swift >/dev/null 2>&1 && swift "$here/icon.swift" "$out/icon.png"; then
    set_dir="$out/AppIcon.iconset"
    mkdir -p "$set_dir"
    for s in 16 32 128 256 512; do
        sips -z $s $s "$out/icon.png" --out "$set_dir/icon_${s}x${s}.png" >/dev/null
        sips -z $((s * 2)) $((s * 2)) "$out/icon.png" --out "$set_dir/icon_${s}x${s}@2x.png" >/dev/null
    done
    iconutil -c icns "$set_dir" -o "$res/droplet.icns"
    rm -f "$res/Assets.car"
    $pb -c "Delete :CFBundleIconName" "$plist" 2>/dev/null || true
else
    echo "warning: swift not found, keeping the default icon (xcode-select --install)" >&2
fi

# Name, identifier, and the files it accepts: audio and video only.
$pb -c "Set :CFBundleName Meeting Transcriber" "$plist"
$pb -c "Add :CFBundleDisplayName string Meeting Transcriber" "$plist"
$pb -c "Add :CFBundleIdentifier string io.github.mionitsa.meeting-transcriber" "$plist"
$pb -c "Delete :CFBundleDocumentTypes" "$plist"
$pb -c "Add :CFBundleDocumentTypes array" \
    -c "Add :CFBundleDocumentTypes:0 dict" \
    -c "Add :CFBundleDocumentTypes:0:CFBundleTypeName string 'Audio or video recording'" \
    -c "Add :CFBundleDocumentTypes:0:CFBundleTypeRole string Viewer" \
    -c "Add :CFBundleDocumentTypes:0:LSHandlerRank string Alternate" \
    -c "Add :CFBundleDocumentTypes:0:LSItemContentTypes array" \
    -c "Add :CFBundleDocumentTypes:0:LSItemContentTypes:0 string public.audio" \
    -c "Add :CFBundleDocumentTypes:0:LSItemContentTypes:1 string public.movie" \
    -c "Add :CFBundleDocumentTypes:0:LSItemContentTypes:2 string public.audiovisual-content" \
    "$plist"

codesign --force --deep --sign - "$app" 2>/dev/null
echo "built $app"

if [ "${1:-}" = "--install" ]; then
    rm -rf "/Applications/Meeting Transcriber.app"
    cp -R "$app" /Applications/
    /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
        -f "/Applications/Meeting Transcriber.app"
    echo "installed /Applications/Meeting Transcriber.app"
fi
