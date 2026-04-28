#!/usr/bin/env bash
# Installs a launchd agent that runs sysdash at login.
# After install, sysdash starts in the background. Open the dashboard with:
#   open "http://localhost:$(cat ~/.sysdash-port)"

set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.sysdash.agent.plist"

mkdir -p "$HOME/Library/LaunchAgents"

# Determine expat lib path for DYLD fix
EXPAT_LIB="/opt/homebrew/opt/expat/lib"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.sysdash.agent</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${DIR}/run.sh</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${DIR}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>DYLD_LIBRARY_PATH</key>
    <string>${EXPAT_LIB}</string>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${DIR}/sysdash.log</string>
  <key>StandardErrorPath</key>
  <string>${DIR}/sysdash.err</string>
</dict>
</plist>
EOF

# Reload
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "[sysdash] launchd agent installed and started."
echo "[sysdash] dashboard URL: http://localhost:\$(cat ~/.sysdash-port)"
echo "[sysdash] uninstall with: launchctl unload $PLIST && rm $PLIST"
