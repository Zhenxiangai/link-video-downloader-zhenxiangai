#!/bin/sh
set -eu

release="v260810"
release_url="https://github.com/ltaoo/wx_channels_download/releases/download/v260810/wx_video_download_v260810_darwin_arm64.zip"
release_sha256="505c6a56dbc2252139c795918a68e4860a6cd62057eefd9fbad7b21a5b6cff6e"
backend_sha256="0e5b490458847b2bb6982f9efa57ccaee7160a92b09734bb892f1aa6de6bbd7c"
model_url="https://huggingface.co/ggerganov/whisper.cpp/resolve/c521a4b02f422512d734391fdf08bb08c0862f68/ggml-small.bin"
model_sha256="1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b"

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
archive_root=${WECHAT_ARCHIVE_ROOT:-"$HOME/Documents/WeChatArchive"}
backend_root=${WECHAT_CHANNELS_HOME:-"$HOME/.local/share/wx_channels_download/$release"}
backend_bin="$backend_root/wx_video_download"
backend_config="$backend_root/config.wechat-archive.yaml"
backend_runtime="$backend_root/runtime"
backend_label="com.wechatarchive.channels"
backend_plist="$HOME/Library/LaunchAgents/$backend_label.plist"
proxy_snapshot="$backend_runtime/proxy-before-capture.env"
model_path=${WECHAT_WHISPER_MODEL:-"$archive_root/models/ggml-small.bin"}
domain="gui/$(id -u)"

PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
export PATH

fail() {
    echo "error=$*" >&2
    exit 1
}

has() {
    command -v "$1" >/dev/null 2>&1
}

hash_ok() {
    [ -f "$1" ] && [ "$(shasum -a 256 "$1" | awk '{print $1}')" = "$2" ]
}

download() {
    python3 - "$1" "$2" <<'PY'
import sys
import urllib.request

url, target = sys.argv[1:]
with urllib.request.urlopen(url) as response, open(target, "wb") as output:
    while chunk := response.read(1024 * 1024):
        output.write(chunk)
PY
}

check_platform() {
    [ "$(uname -s)" = "Darwin" ] || fail "unsupported_platform: macOS required"
    [ "$(uname -m)" = "arm64" ] || fail "unsupported_arch: Apple Silicon required"
}

api_get() {
    /usr/bin/curl --fail --silent --max-time 3 "http://127.0.0.1:2022$1"
}

api_post() {
    /usr/bin/curl --fail --silent --max-time 20 \
        -H 'Content-Type: application/json' \
        -d "$2" "http://127.0.0.1:2022$1" |
        python3 -c 'import json,sys; data=json.load(sys.stdin); code=data.get("code"); code == 0 or (_ for _ in ()).throw(SystemExit(data.get("msg", "API failed")))'
}

doctor() {
    echo "platform=$(uname -s)"
    echo "arch=$(uname -m)"
    for command_name in hermes python3 brew ffmpeg whisper-cli; do
        if has "$command_name"; then
            echo "$command_name=$(command -v "$command_name")"
        else
            echo "$command_name=missing"
        fi
    done
    if hash_ok "$model_path" "$model_sha256"; then
        echo "whisper_model=ready"
    elif [ -f "$model_path" ]; then
        echo "whisper_model=checksum_mismatch"
    else
        echo "whisper_model=missing"
    fi
    if hash_ok "$backend_bin" "$backend_sha256"; then
        echo "channels_backend_binary=$backend_bin"
    elif [ -f "$backend_bin" ]; then
        echo "channels_backend_binary=checksum_mismatch"
    else
        echo "channels_backend_binary=missing"
    fi
    if has hermes && hermes computer-use status >/dev/null 2>&1; then
        echo "computer_use=installed"
    else
        echo "computer_use=missing"
    fi
}

install_dependencies() {
    brew_command=$(command -v brew 2>/dev/null || true)
    [ -n "$brew_command" ] || {
        echo "action_required=homebrew_missing"
        echo "message=Hermes must review the official Homebrew installer, explain it, request approval, install Homebrew, then run this command again."
        exit 69
    }
    missing=""
    has ffmpeg || missing="ffmpeg"
    has whisper-cli || missing="$missing whisper-cpp"
    [ -z "$missing" ] || "$brew_command" install $missing
}

install_model() {
    mkdir -p "$(dirname -- "$model_path")"
    if hash_ok "$model_path" "$model_sha256"; then
        return
    fi
    [ ! -f "$model_path" ] || fail "model_checksum_mismatch: $model_path"
    temporary="$model_path.download"
    download "$model_url" "$temporary"
    hash_ok "$temporary" "$model_sha256" || fail "downloaded_model_checksum_mismatch: $temporary"
    mv "$temporary" "$model_path"
    chmod 600 "$model_path"
}

install_computer_use() {
    hermes tools enable computer_use
    if ! hermes computer-use status >/dev/null 2>&1; then
        hermes computer-use install
    fi
}

write_backend_config() {
    [ -f "$backend_config" ] && return
    mkdir -p "$backend_runtime" "$archive_root/video_channels"
    umask 077
    cat >"$backend_config" <<EOF
workdir: "$backend_runtime"
api:
  protocol: http
  hostname: 127.0.0.1
  port: 2022
db:
  type: sqlite
  filepath: "$backend_runtime/data.db"
cert:
  file: "$backend_runtime/certs/wechat-archive.pem"
  key: "$backend_runtime/certs/wechat-archive.key"
  name: wechat_archive_local
channels:
  enabled: true
  refreshinterval: 0
  download:
    cover: false
    defaulthighest: true
    forcecheckallfeeds: false
    frontend: false
    pausewhendownload: false
download:
  dir: "$archive_root/video_channels"
  filenametemplate: '{{filename}}_{{spec}}'
  playdoneaudio: false
proxy:
  enabled: false
  system: false
  hostname: 127.0.0.1
  port: 2023
  skipinstallrootcert: true
  upstreamproxy: ""
  tun: false
  tcprelay:
    enabled: false
    hostname: 127.0.0.1
    port: 9900
mp:
  enabled: true
  refreshskipminutes: 20
EOF
}

ensure_mp_enabled() {
    python3 - "$backend_config" <<'PY'
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines()
start = next((i for i, line in enumerate(lines) if line == "mp:"), None)
if start is None:
    lines.extend(["mp:", "  enabled: true", "  refreshskipminutes: 20"])
else:
    end = next((i for i in range(start + 1, len(lines)) if lines[i] and not lines[i][0].isspace()), len(lines))
    enabled = next((i for i in range(start + 1, end) if lines[i].strip().startswith("enabled:")), None)
    if enabled is None:
        lines.insert(start + 1, "  enabled: true")
    else:
        lines[enabled] = lines[enabled][: len(lines[enabled]) - len(lines[enabled].lstrip())] + "enabled: true"
temporary = path.with_suffix(path.suffix + ".tmp")
temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
os.chmod(temporary, 0o600)
os.replace(temporary, path)
PY
}

install_backend() {
    if [ -f "$backend_bin" ] && ! hash_ok "$backend_bin" "$backend_sha256"; then
        fail "channels_backend_binary_checksum_mismatch: $backend_bin"
    fi
    if [ ! -x "$backend_bin" ]; then
        temporary_dir=$(mktemp -d)
        archive="$temporary_dir/backend.zip"
        download "$release_url" "$archive"
        hash_ok "$archive" "$release_sha256" || fail "channels_backend_checksum_mismatch: $archive"
        unzip -q "$archive" -d "$temporary_dir/unpacked"
        source_bin=$(find "$temporary_dir/unpacked" -type f -name wx_video_download -print | head -n 1)
        [ -n "$source_bin" ] || fail "channels_backend_binary_missing_in_release"
        hash_ok "$source_bin" "$backend_sha256" || fail "channels_backend_binary_checksum_mismatch: $source_bin"
        mkdir -p "$backend_root"
        cp "$source_bin" "$backend_bin"
        chmod 755 "$backend_bin"
        source_license=$(find "$temporary_dir/unpacked" -type f -name LICENSE -print | head -n 1)
        [ -z "$source_license" ] || cp "$source_license" "$backend_root/LICENSE"
        find "$temporary_dir" -depth -delete
    fi
    write_backend_config
    ensure_mp_enabled
}

plist_add() {
    /usr/libexec/PlistBuddy -c "$1" "$backend_plist"
}

install_backend_service() {
    mkdir -p "$HOME/Library/LaunchAgents" "$backend_runtime"
    launchctl bootout "$domain/$backend_label" 2>/dev/null || true
    [ ! -f "$backend_plist" ] || find "$backend_plist" -type f -delete
    plutil -create xml1 "$backend_plist"
    plist_add "Add :Label string $backend_label"
    plist_add "Add :ProgramArguments array"
    plist_add "Add :ProgramArguments:0 string $backend_bin"
    plist_add "Add :ProgramArguments:1 string -c"
    plist_add "Add :ProgramArguments:2 string $backend_config"
    plist_add "Add :ProgramArguments:3 string server"
    plist_add "Add :RunAtLoad bool true"
    plist_add "Add :KeepAlive bool true"
    plist_add "Add :ProcessType string Background"
    plist_add "Add :StandardOutPath string $backend_runtime/backend.stdout.log"
    plist_add "Add :StandardErrorPath string $backend_runtime/backend.stderr.log"
    chmod 644 "$backend_plist"
    plutil -lint "$backend_plist"
    launchctl bootstrap "$domain" "$backend_plist"
    launchctl enable "$domain/$backend_label"
    launchctl kickstart -k "$domain/$backend_label"
    attempts=0
    until api_get /api/status >/dev/null 2>&1; do
        attempts=$((attempts + 1))
        [ "$attempts" -lt 20 ] || fail "channels_backend_did_not_start"
        sleep 1
    done
}

install_all() {
    check_platform
    has hermes || fail "hermes_missing"
    has python3 || fail "python3_missing"
    install_computer_use
    install_dependencies
    install_model
    install_backend
    install_backend_service
    WECHAT_ARCHIVE_ROOT="$archive_root" WECHAT_WHISPER_MODEL="$model_path" \
        sh "$script_dir/manage_transcriber.sh" install
    WECHAT_WORKER_KIND=content WECHAT_ARCHIVE_ROOT="$archive_root" WECHAT_WHISPER_MODEL="$model_path" \
        sh "$script_dir/manage_transcriber.sh" install
    status
}

proxy_field() {
    networksetup "$1" "$2" | awk -F': ' -v wanted="$3" '$1 == wanted {print $2; exit}'
}

active_service() {
    interface=$(route -n get default 2>/dev/null | awk '/interface:/{print $2; exit}')
    networksetup -listallhardwareports | awk -v wanted="$interface" '
        /^Hardware Port:/ {sub(/^Hardware Port: /, ""); port=$0}
        /^Device:/ {sub(/^Device: /, ""); if ($0 == wanted) {print port; exit}}
    '
}

snapshot_proxy() {
    [ -f "$proxy_snapshot" ] && return
    service=$(active_service)
    [ -n "$service" ] || fail "active_network_service_not_found"
    mkdir -p "$backend_runtime"
    umask 077
    {
        echo "service=$service"
        echo "web_enabled=$(proxy_field -getwebproxy "$service" Enabled)"
        echo "web_server=$(proxy_field -getwebproxy "$service" Server)"
        echo "web_port=$(proxy_field -getwebproxy "$service" Port)"
        echo "secure_enabled=$(proxy_field -getsecurewebproxy "$service" Enabled)"
        echo "secure_server=$(proxy_field -getsecurewebproxy "$service" Server)"
        echo "secure_port=$(proxy_field -getsecurewebproxy "$service" Port)"
        echo "socks_enabled=$(proxy_field -getsocksfirewallproxy "$service" Enabled)"
        echo "socks_server=$(proxy_field -getsocksfirewallproxy "$service" Server)"
        echo "socks_port=$(proxy_field -getsocksfirewallproxy "$service" Port)"
    } >"$proxy_snapshot"
}

snapshot_value() {
    sed -n "s/^$1=//p" "$proxy_snapshot" | head -n 1
}

enable_capture() {
    check_platform
    api_get /api/status >/dev/null || fail "channels_backend_unavailable"
    snapshot_proxy
    service=$(snapshot_value service)
    upstream=""
    if [ "$(snapshot_value web_enabled)" = "Yes" ]; then
        upstream="http://$(snapshot_value web_server):$(snapshot_value web_port)"
    elif [ "$(snapshot_value secure_enabled)" = "Yes" ]; then
        upstream="http://$(snapshot_value secure_server):$(snapshot_value secure_port)"
    elif [ "$(snapshot_value socks_enabled)" = "Yes" ]; then
        upstream="socks5://$(snapshot_value socks_server):$(snapshot_value socks_port)"
    fi
    cert_name="wechat_archive_$(id -u)_$(date -u +%Y%m%dT%H%M%SZ)"
    api_post /api/proxy/certificate/generate "{\"name\":\"$cert_name\",\"valid_years\":1,\"install\":false,\"restart\":false}"
    cert_file="$backend_runtime/certs/$cert_name.pem"
    [ -f "$cert_file" ] || fail "generated_certificate_not_found"
    security add-trusted-cert -d -r trustRoot -k "$HOME/Library/Keychains/login.keychain-db" "$cert_file"
    proxy_json=$(python3 - "$service" "$upstream" <<'PY'
import json
import sys

service, upstream = sys.argv[1:]
print(json.dumps({"values": {
    "proxy.enabled": True,
    "proxy.system": True,
    "proxy.defaultInterface": service,
    "proxy.hostname": "127.0.0.1",
    "proxy.port": 2023,
    "proxy.skipInstallRootCert": True,
    "proxy.upstreamProxy": upstream,
}, "restart": True}))
PY
)
    api_post /api/proxy/config "$proxy_json"
    echo "capture=enabled"
    echo "action_required=open_wechat_login_and_requested_content"
}

restore_proxy() {
    [ -f "$proxy_snapshot" ] || return
    service=$(snapshot_value service)
    if [ "$(snapshot_value web_enabled)" = "Yes" ]; then
        networksetup -setwebproxy "$service" "$(snapshot_value web_server)" "$(snapshot_value web_port)"
        networksetup -setwebproxystate "$service" on
    else
        networksetup -setwebproxystate "$service" off
    fi
    if [ "$(snapshot_value secure_enabled)" = "Yes" ]; then
        networksetup -setsecurewebproxy "$service" "$(snapshot_value secure_server)" "$(snapshot_value secure_port)"
        networksetup -setsecurewebproxystate "$service" on
    else
        networksetup -setsecurewebproxystate "$service" off
    fi
}

disable_capture() {
    api_get /api/status >/dev/null || fail "channels_backend_unavailable"
    api_post /api/proxy/config '{"values":{"proxy.enabled":false,"proxy.system":false},"restart":true}'
    restore_proxy
    echo "capture=disabled"
    echo "previous_proxy=restored"
}

status() {
    doctor
    if api_get /api/status >/dev/null 2>&1; then
        api_get /api/status | python3 -c 'import json,sys; d=json.load(sys.stdin)["data"]; print("channels_api=" + d["api"]["status"]); print("capture_proxy=" + d["proxy"]["status"])'
    else
        echo "channels_api=stopped"
        echo "capture_proxy=stopped"
    fi
    if launchctl print "$domain/com.wechatarchive.transcriber" >/dev/null 2>&1; then
        echo "transcriber=running"
        WECHAT_ARCHIVE_ROOT="$archive_root" sh "$script_dir/manage_transcriber.sh" status
    else
        echo "transcriber=stopped"
    fi
    if launchctl print "$domain/com.wechatarchive.content" >/dev/null 2>&1; then
        echo "content_worker=running"
        WECHAT_WORKER_KIND=content WECHAT_ARCHIVE_ROOT="$archive_root" sh "$script_dir/manage_transcriber.sh" status
    else
        echo "content_worker=stopped"
    fi
}

case "${1:-}" in
    doctor) doctor ;;
    install) install_all ;;
    status) status ;;
    enable-capture) enable_capture ;;
    disable-capture) disable_capture ;;
    *)
        echo "usage: $0 {doctor|install|status|enable-capture|disable-capture}" >&2
        exit 64
        ;;
esac
