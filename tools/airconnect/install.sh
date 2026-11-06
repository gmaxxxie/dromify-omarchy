#!/usr/bin/env bash
# Install the AirConnect (airupnp) AirPlay 1 → UPnP/DLNA bridge as a macOS
# LaunchAgent, wrapped in a signed .app so the Application Firewall and the
# Local Network privacy prompt attribute it to a real bundle.
#
# Usage:
#   tools/airconnect/install.sh /path/to/airupnp-macos-arm64-static [LAN_IP]
#
# Get the binary from the AirConnect release zip:
#   curl -LO https://raw.githubusercontent.com/philippe44/AirConnect/master/AirConnect-1.12.4.zip
#   unzip -o AirConnect-1.12.4.zip airupnp-macos-arm64-static
#
# Always use the *-static build: the dynamic one aborts on recent macOS with
# "loading libcrypto in an unsafe way".
set -euo pipefail

BIN="${1:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/airconnect/AirConnect.app"
AGENT="$HOME/Library/LaunchAgents/com.maxwei.airconnect.plist"
LABEL="com.maxwei.airconnect"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[[ -n "$BIN" ]] || die "usage: $0 /path/to/airupnp-macos-arm64-static [LAN_IP]"
[[ -f "$BIN" ]] || die "binary not found: $BIN"
# The dynamic build links OpenSSL and aborts on recent macOS ("loading libcrypto
# in an unsafe way"); detect that rather than trusting the file name.
if otool -L "$BIN" 2>/dev/null | grep -q "libcrypto"; then
  die "this binary links libcrypto dynamically; use the -static build"
fi

# Bind to the LAN interface, never to the tunnel: with a system proxy (SFM/sing-box)
# or Tailscale present, a bare autodetect can pick utun* and mDNS then goes nowhere.
LAN_IP="${2:-}"
if [[ -z "$LAN_IP" ]]; then
  LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || true)"
  [[ -n "$LAN_IP" ]] || die "pass the LAN IP explicitly (ipconfig getifaddr en0 was empty)"
fi

echo "==> building $APP (bind $LAN_IP)"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
install -m 0755 "$BIN" "$APP/Contents/Resources/airupnp-raw"

cat > "$APP/Contents/MacOS/AirConnect" <<EOF
#!/bin/sh
DIR="\$(dirname "\$0")/../Resources"
LOG="\$HOME/Library/Logs/airconnect.log"
echo "=== \$(date +%F_%T) launch (pid \$\$)" >> "\$LOG"
exec "\$DIR/airupnp-raw" -Z -x "\$DIR/config.xml" -b $LAN_IP -c pcm >> "\$LOG" 2>&1
EOF
chmod 0755 "$APP/Contents/MacOS/AirConnect"

cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>AirConnect</string>
  <key>CFBundleDisplayName</key><string>AirConnect</string>
  <key>CFBundleIdentifier</key><string>com.maxwei.airconnect</string>
  <key>CFBundleVersion</key><string>1.12.4</string>
  <key>CFBundleShortVersionString</key><string>1.12.4</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>AirConnect</string>
  <key>LSBackgroundOnly</key><true/>
  <key>LSUIElement</key><true/>
</dict></plist>
EOF

sed "s/__LAN_IP__/$LAN_IP/g" "$HERE/config.xml" > "$APP/Contents/Resources/config.xml"

# Ad-hoc signature: without a stable bundle identity the Application Firewall
# silently drops every inbound connection to the bridge's HTTP/RTSP ports.
codesign --force --deep -s - "$APP" >/dev/null 2>&1 || die "codesign failed"

echo "==> writing LaunchAgent $AGENT"
mkdir -p "$(dirname "$AGENT")"
sed -e "s#__HOME__#$HOME#g" -e "s#__LAN_IP__#$LAN_IP#g" \
  "$HERE/com.maxwei.airconnect.plist" > "$AGENT"
plutil -lint "$AGENT" >/dev/null

if [[ "${NO_LAUNCH:-0}" == "1" ]]; then
  echo "==> NO_LAUNCH=1: skipping launchctl bootstrap"
else
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$AGENT"
  sleep 15
  echo
  echo "==> log tail"
  tail -n 6 "$HOME/Library/Logs/airconnect-launchd.log" 2>/dev/null || tail -n 6 "$HOME/Library/Logs/airconnect.log" 2>/dev/null || true
fi
echo
cat <<'EOF'
Next steps:
  1. "SRS-ZR7 ..." must appear in the AirPlay menu of the Music app.
     If it does not: System Settings → Privacy & Security → Local Network →
     enable AirConnect (one-time; launchd contexts are gated until then).
  2. Diagnose timing and dropouts with tools/airconnect/measure.py and stalls.py.
  3. The bridge's AirPlay device id changes on every restart — reselect the
     output in the Music app after each restart.
EOF
