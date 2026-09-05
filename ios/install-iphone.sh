#!/usr/bin/env bash
set -euo pipefail

# Install ReceiptSync on a USB-connected iPhone with a free Apple ID.
# A paid Apple Developer Program membership is not required.

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project="$script_dir/ReceiptSync.xcodeproj"
support="$script_dir/install_iphone_support.py"
env_file="$script_dir/install-local.env"
derived_data="$script_dir/build-device"
helper=(python3 "$support")

team="${RECEIPT_SYNC_DEVELOPMENT_TEAM:-}"
bundle="${RECEIPT_SYNC_BUNDLE_ID:-}"
device="${RECEIPT_SYNC_DEVICE_UDID:-}"
skip_launch=0

usage() {
  cat <<'EOF'
Install ReceiptSync onto your iPhone over USB. Uses a free Apple ID.

Usage:
  ./ios/install-iphone.sh
  ./ios/install-iphone.sh --team TEAMID --bundle com.example.receiptsync
  ./ios/install-iphone.sh --device UDID

You do not need a paid Apple Developer account. Sign the same free Apple ID
you already use on the iPhone into Xcode once, plug in the phone, then rerun.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --team)
      team=$2
      shift 2
      ;;
    --bundle)
      bundle=$2
      shift 2
      ;;
    --device)
      device=$2
      shift 2
      ;;
    --skip-launch)
      skip_launch=1
      shift
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

pause() {
  if [[ -t 0 ]]; then
    read -r -p "Press Enter after you finish that step. "
  else
    echo "Connect the iPhone and sign into Xcode, then run this installer again." >&2
    exit 1
  fi
}

open_xcode_accounts() {
  open -a Xcode "$project"
  osascript >/dev/null 2>&1 <<'APPLESCRIPT' || true
tell application "Xcode" to activate
delay 0.8
tell application "System Events"
  tell process "Xcode"
    try
      click menu item "Settings…" of menu "Xcode" of menu bar 1
    on error
      try
        click menu item "Settings..." of menu "Xcode" of menu bar 1
      end try
    end try
  end tell
end tell
APPLESCRIPT
}

explain_missing_team() {
  cat <<'EOF'
Xcode still has no Apple ID, so there is no Personal Team yet.
Opening the project or Devices and Simulators is not enough.

1. Click the Xcode window, then press Command + comma (⌘,).
   Or use the menu: Xcode → Settings… → Apple Accounts (sometimes labeled Accounts).
2. Click + at the bottom left and choose Apple ID.
3. Sign in with the same free Apple ID already on your iPhone.
   Approve the two-factor code if Apple sends one.
4. Success looks like this, before you press Enter:
   Left side: your Apple ID
   Right side: a team such as "Your Name (Personal Team)"
5. Do not buy a developer membership.

EOF
}

pick_row() {
  local prompt=$1
  local count=$2
  local choice
  if [[ "$count" -eq 1 ]]; then
    printf '%s\n' 1
    return 0
  fi
  if [[ ! -t 0 ]]; then
    echo "More than one $prompt is available. Pass --team / --device explicitly." >&2
    exit 1
  fi
  read -r -p "Choose a $prompt [1-$count]: " choice
  if [[ ! "$choice" =~ ^[0-9]+$ ]] || [[ "$choice" -lt 1 ]] || [[ "$choice" -gt "$count" ]]; then
    echo "Invalid $prompt choice." >&2
    exit 1
  fi
  printf '%s\n' "$choice"
}

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "iPhone install has to run on this Mac. Windows cannot sign the iOS app." >&2
  exit 1
fi

if [[ ! -d "$project" ]]; then
  echo "Missing Xcode project: $project" >&2
  exit 1
fi

if ! command -v xcodebuild >/dev/null 2>&1; then
  echo "Xcode is not installed. Install Xcode from the Mac App Store, open it once, then rerun." >&2
  exit 1
fi

if ! xcodebuild -checkFirstLaunchStatus >/dev/null 2>&1; then
  echo "Open Xcode once and finish its first-launch license agreement, then rerun this installer."
  open -a Xcode "$project"
  pause
  if ! xcodebuild -checkFirstLaunchStatus >/dev/null 2>&1; then
    echo "Xcode first-launch is still incomplete." >&2
    exit 1
  fi
fi

if [[ -z "$team" || -z "$bundle" || -z "$device" ]]; then
  while IFS='=' read -r key value; do
    case "$key" in
      DEVELOPMENT_TEAM)
        team=${team:-$value}
        ;;
      PRODUCT_BUNDLE_IDENTIFIER)
        bundle=${bundle:-$value}
        ;;
      DEVICE_UDID)
        device=${device:-$value}
        ;;
    esac
  done < <("${helper[@]}" dump-env "$env_file")
fi

echo "ReceiptSync iPhone installer"
echo "A paid Apple Developer Program account is not required."
echo

load_teams() {
  "${helper[@]}" list-teams --pbxproj "$project/project.pbxproj" --env "$env_file"
}

load_phones() {
  "${helper[@]}" list-iphones
}

if [[ -z "$team" ]]; then
  teams=()
  while IFS= read -r line; do
    [[ -n "$line" ]] && teams+=("$line")
  done < <(load_teams)
  if [[ ${#teams[@]} -eq 0 ]]; then
    explain_missing_team
    open_xcode_accounts
    while [[ ${#teams[@]} -eq 0 ]]; do
      pause
      teams=()
      while IFS= read -r line; do
        [[ -n "$line" ]] && teams+=("$line")
      done < <(load_teams)
      if [[ ${#teams[@]} -eq 0 ]]; then
        echo "Still no Personal Team. The Apple ID is not in Xcode Accounts yet."
        echo
        explain_missing_team
        open_xcode_accounts
      fi
    done
  fi
  echo "Available signing teams:"
  index=1
  for row in "${teams[@]}"; do
    IFS=$'\t' read -r team_id label source <<<"$row"
    echo "  $index) $team_id  ($label, $source)"
    index=$((index + 1))
  done
  choice=$(pick_row "team" ${#teams[@]})
  IFS=$'\t' read -r team _label _source <<<"${teams[$((choice - 1))]}"
fi

if [[ -z "$bundle" ]]; then
  bundle=$("${helper[@]}" suggest-bundle "$team")
fi

if [[ -z "$device" ]]; then
  phones=()
  while IFS= read -r line; do
    [[ -n "$line" ]] && phones+=("$line")
  done < <(load_phones)
  if [[ ${#phones[@]} -eq 0 ]]; then
    cat <<'EOF'
No iPhone is visible to this Mac yet.

1. Use a USB cable (not only Wi-Fi).
2. Unlock the iPhone and tap Trust if asked.
3. Open Xcode → Window → Devices and Simulators and wait until this iPhone appears.
   Developer Mode is hidden until Xcode has seen the phone. After that, look at
   the bottom of Settings → Privacy & Security (设置 → 隐私与安全性).
   It is not under General. If it still is missing, continue; the first install
   attempt often makes the toggle appear.

EOF
    pause
    while IFS= read -r line; do
      [[ -n "$line" ]] && phones+=("$line")
    done < <(load_phones)
  fi
  if [[ ${#phones[@]} -eq 0 ]]; then
    echo "Still no iPhone. Unlock it, keep it on the lock screen dismissed, and rerun." >&2
    exit 1
  fi
  echo "Connected iPhones:"
  index=1
  for row in "${phones[@]}"; do
    IFS=$'\t' read -r udid name os available <<<"$row"
    state="ready"
    [[ "$available" == "0" ]] && state="locked or pairing"
    echo "  $index) $name  iOS ${os:-unknown}  ($state)"
    index=$((index + 1))
  done
  choice=$(pick_row "iPhone" ${#phones[@]})
  IFS=$'\t' read -r device _name _os _available <<<"${phones[$((choice - 1))]}"
fi

"${helper[@]}" write-env "$env_file" --team "$team" --bundle "$bundle" --device "$device"
echo "Signing with Personal Team $team"
echo "Bundle ID $bundle"
echo "Installing to device $device"
echo "Xcode Accounts may still show 0 devices. That is normal for a free Apple ID."
echo "This first install is what registers the connected iPhone with that team."
echo "Keep the iPhone unlocked. The first build may take a few minutes."
echo

mkdir -p "$derived_data"
set +e
xcodebuild \
  -project "$project" \
  -scheme ReceiptSync \
  -configuration Debug \
  -destination "id=$device" \
  -derivedDataPath "$derived_data" \
  -allowProvisioningUpdates \
  -allowProvisioningDeviceRegistration \
  DEVELOPMENT_TEAM="$team" \
  PRODUCT_BUNDLE_IDENTIFIER="$bundle" \
  CODE_SIGN_STYLE=Automatic \
  CODE_SIGN_IDENTITY="Apple Development" \
  build
build_status=$?
set -e

if [[ "$build_status" -ne 0 ]]; then
  cat <<EOF >&2
The iPhone build failed.

Common first-time fixes:
- In Xcode → Settings → Accounts, keep the free Apple ID signed in.
- Unlock the iPhone and tap Trust.
- Enable Developer Mode, then reconnect the cable.
- If Apple rejects the bundle id, rerun with:
  ./ios/install-iphone.sh --bundle com.receiptsync.uniqueid
EOF
  exit "$build_status"
fi

app_path=$(find "$derived_data/Build/Products" -path "*/Debug-iphoneos/ReceiptSync.app" -print -quit || true)
if [[ -z "$app_path" ]]; then
  echo "The build finished, but ReceiptSync.app was not found under $derived_data." >&2
  exit 1
fi

echo "Installing $app_path"
if ! xcrun devicectl device install app --device "$device" "$app_path"; then
  echo "Automatic install failed. I will open Xcode; select your iPhone as the run destination and press Run." >&2
  open -a Xcode "$project"
  exit 1
fi

if [[ "$skip_launch" -eq 0 ]]; then
  xcrun devicectl device process launch --device "$device" "$bundle" >/dev/null 2>&1 || true
fi

cat <<EOF

Installed. On the iPhone, open 小票同步.

If iOS blocks the app:
  Settings → General → VPN & Device Management
  设置 → 通用 → VPN与设备管理
  Trust this Apple ID, then open the app again.

Free Apple ID installs usually expire after about 7 days. Plug the phone in and
run this installer again; you still do not need a paid developer account.

Next:
1. On this Mac start the computer service with start-service.command or ./pc/start.sh
2. Open http://127.0.0.1:8764 and copy the current pairing address, key, and fingerprint
3. Enter those three values in the iPhone app under 同步设置

Saved local signing values: $env_file
Do not commit that file, and do not write the pairing key into the project.
EOF
