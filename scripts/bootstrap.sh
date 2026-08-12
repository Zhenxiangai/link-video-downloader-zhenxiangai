#!/bin/sh
set -eu

release="v260810"
release_url="https://github.com/ltaoo/wx_channels_download/releases/download/v260810/wx_video_download_v260810_darwin_arm64.zip"
release_sha256="505c6a56dbc2252139c795918a68e4860a6cd62057eefd9fbad7b21a5b6cff6e"
backend_sha256="0e5b490458847b2bb6982f9efa57ccaee7160a92b09734bb892f1aa6de6bbd7c"
model_url="https://huggingface.co/ggerganov/whisper.cpp/resolve/c521a4b02f422512d734391fdf08bb08c0862f68/ggml-small.bin"
model_sha256="1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b"
core_revision="8c137bf1a56106a050f12567fe0ed587bccea042"
core_url="https://codeload.github.com/Zhenxiangai/link-video-downloader-zhenxiangai/tar.gz/$core_revision"
core_sha256="acccec7f474bfc605fe01113e2d06b28908c1602e877c5aa0985db39d6cb20d2"

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
hermes_home=${HERMES_HOME:-"$HOME/.hermes"}
archive_root=${WECHAT_ARCHIVE_ROOT:-"$HOME/Documents/WeChatArchive"}
backend_root=${WECHAT_CHANNELS_HOME:-"$HOME/.local/share/wx_channels_download/$release"}
backend_bin="$backend_root/wx_video_download"
backend_config="$backend_root/config.wechat-archive.yaml"
backend_runtime="$backend_root/runtime"
backend_label="com.wechatarchive.channels"
backend_plist="$HOME/Library/LaunchAgents/$backend_label.plist"
proxy_snapshot="$backend_runtime/proxy-before-capture.env"
clash_socket="/tmp/verge/verge-mihomo.sock"
clash_config="$HOME/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/clash-verge.yaml"
clash_capture_proxy="wechat_archive_capture"
clash_default_group="wechat_archive_default"
model_path=${WECHAT_WHISPER_MODEL:-"$archive_root/models/ggml-small.bin"}
core_root="$HOME/.local/share/wechat-archive/transparent-core/$core_revision"
domain="gui/$(id -u)"

PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
export PATH

if [ -x "$hermes_home/hermes-agent/venv/bin/python" ]; then
    python_bin="$hermes_home/hermes-agent/venv/bin/python"
else
    python_bin=$(command -v python3 2>/dev/null || true)
fi

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

core_hash() {
    "$python_bin" - "$script_dir/wechat_archive.py" "$1" <<'PY'
import runpy
import sys
from pathlib import Path

module = runpy.run_path(sys.argv[1])
print(module["transparent_core_sha256"](Path(sys.argv[2])))
PY
}

core_ok() {
    [ -d "$1" ] && [ "$(core_hash "$1" 2>/dev/null || true)" = "$core_sha256" ]
}

download() {
    "$python_bin" - "$1" "$2" <<'PY'
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
    /usr/bin/curl --fail --silent --noproxy '*' --max-time 3 "http://127.0.0.1:2022$1"
}

api_post() {
    /usr/bin/curl --fail --silent --noproxy '*' --max-time 20 \
        -H 'Content-Type: application/json' \
        -d "$2" "http://127.0.0.1:2022$1" |
        "$python_bin" -c 'import json,sys; data=json.load(sys.stdin); code=data.get("code"); code == 0 or (_ for _ in ()).throw(SystemExit(data.get("msg", "API failed")))'
}

doctor() {
    echo "platform=$(uname -s)"
    echo "arch=$(uname -m)"
    for command_name in hermes brew ffmpeg whisper-cli; do
        if has "$command_name"; then
            echo "$command_name=$(command -v "$command_name")"
        else
            echo "$command_name=missing"
        fi
    done
    if [ -n "$python_bin" ] && [ -x "$python_bin" ]; then
        echo "python=$python_bin"
    else
        echo "python=missing"
    fi
    if [ -d "/Applications/WeChat.app" ] || [ -d "$HOME/Applications/WeChat.app" ]; then
        echo "wechat_app=ready"
    else
        echo "wechat_app=missing"
    fi
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
    bundled_core="$script_dir/../vendor/transparent-core"
    if [ -e "$bundled_core" ]; then
        active_core="$bundled_core"
    else
        active_core="$core_root"
    fi
    if core_ok "$active_core"; then
        echo "transparent_core=ready"
    elif [ -e "$active_core" ]; then
        echo "transparent_core=checksum_mismatch"
    else
        echo "transparent_core=missing"
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

install_core() {
    bundled_core="$script_dir/../vendor/transparent-core"
    if [ -e "$bundled_core" ]; then
        core_ok "$bundled_core" || fail "transparent_core_checksum_mismatch: $bundled_core"
        return
    fi
    core_ok "$core_root" && return
    [ ! -e "$core_root" ] || fail "transparent_core_checksum_mismatch: $core_root"
    parent=$(dirname -- "$core_root")
    mkdir -p "$parent"
    temporary_dir=$(mktemp -d "$parent/.install.XXXXXX")
    archive="$temporary_dir/source.tar.gz"
    download "$core_url" "$archive"
    tar -xzf "$archive" -C "$temporary_dir"
    source_core=$(find "$temporary_dir" -mindepth 3 -maxdepth 3 -type d -path '*/vendor/transparent-core' -print -quit)
    [ -n "$source_core" ] && core_ok "$source_core" || {
        find "$temporary_dir" -depth -delete
        fail "downloaded_transparent_core_checksum_mismatch"
    }
    mv "$source_core" "$core_root"
    find "$temporary_dir" -depth -delete
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
    "$python_bin" - "$backend_config" <<'PY'
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
    [ -n "$python_bin" ] && [ -x "$python_bin" ] || fail "python_missing"
    install_computer_use
    install_dependencies
    install_model
    install_core
    install_backend
    install_backend_service
    WECHAT_PYTHON="$python_bin" WECHAT_ARCHIVE_ROOT="$archive_root" WECHAT_WHISPER_MODEL="$model_path" \
        sh "$script_dir/manage_transcriber.sh" install
    WECHAT_WORKER_KIND=content WECHAT_PYTHON="$python_bin" WECHAT_ARCHIVE_ROOT="$archive_root" WECHAT_WHISPER_MODEL="$model_path" \
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
    [ ! -f "$proxy_snapshot" ] || fail "capture_transaction_already_active: run disable-capture first"
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

clash_get() {
    [ -S "$clash_socket" ] || return 1
    /usr/bin/curl --fail --silent --max-time 3 --unix-socket "$clash_socket" "http://localhost$1"
}

clash_is_system_proxy() {
    service=$1
    config=$(clash_get /configs) || return 1
    "$python_bin" - "$config" \
        "$(proxy_field -getwebproxy "$service" Enabled)" "$(proxy_field -getwebproxy "$service" Server)" "$(proxy_field -getwebproxy "$service" Port)" \
        "$(proxy_field -getsecurewebproxy "$service" Enabled)" "$(proxy_field -getsecurewebproxy "$service" Server)" "$(proxy_field -getsecurewebproxy "$service" Port)" \
        "$(proxy_field -getsocksfirewallproxy "$service" Enabled)" "$(proxy_field -getsocksfirewallproxy "$service" Server)" "$(proxy_field -getsocksfirewallproxy "$service" Port)" <<'PY'
import json
import sys

config = json.loads(sys.argv[1])
mixed_port = str(config.get("mixed-port") or "")
pairs = zip(sys.argv[2::3], sys.argv[3::3], sys.argv[4::3])
enabled = [(host, port) for state, host, port in pairs if state == "Yes"]
uses_clash = bool(enabled) and all(
    host in {"127.0.0.1", "localhost", "::1"} and port == mixed_port
    for host, port in enabled
)
raise SystemExit(0 if mixed_port and uses_clash else 1)
PY
}

clash_capture_ready() {
    service=$1
    clash_is_system_proxy "$service" || return 1
    rules=$(clash_get /rules) || return 1
    clash_get "/proxies/$clash_capture_proxy" >/dev/null || return 1
    "$python_bin" - "$rules" "$clash_capture_proxy" <<'PY'
import json
import sys

rules = json.loads(sys.argv[1]).get("rules") or []
target = sys.argv[2]
has_rule = any(
    rule.get("type") == "DomainSuffix"
    and rule.get("payload") == "qq.com"
    and rule.get("proxy") == target
    for rule in rules
)
raise SystemExit(0 if has_rule else 1)
PY
}

clash_put() {
    /usr/bin/curl --fail --silent --output /dev/null --max-time 60 \
        --unix-socket "$clash_socket" -X PUT -H 'Content-Type: application/json' \
        --data-binary @- 'http://localhost/configs?force=true'
}

clash_patch() {
    /usr/bin/curl --fail --silent --output /dev/null --max-time 60 \
        --unix-socket "$clash_socket" -X PATCH -H 'Content-Type: application/json' \
        --data-binary @- 'http://localhost/configs'
}

enable_clash_capture() {
    [ -f "$clash_config" ] || return 1
    payload=$("$python_bin" - "$clash_config" "$clash_socket" "$clash_capture_proxy" "$clash_default_group" <<'PY'
import http.client
import json
import socket
import sys
from pathlib import Path

import yaml

config_path, socket_path, capture_proxy, default_group = sys.argv[1:]

class UnixHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(socket_path)

def unix_get(path):
    connection = UnixHTTPConnection("localhost", timeout=3)
    connection.request("GET", path)
    response = connection.getresponse()
    if response.status != 200:
        raise SystemExit(f"Mihomo API returned HTTP {response.status}")
    return json.loads(response.read())

config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
if not isinstance(config, dict):
    raise SystemExit("invalid Clash configuration")
proxies = config.get("proxies") or []
groups = config.get("proxy-groups") or []
reserved = {capture_proxy, default_group}
if any(item.get("name") in reserved for item in [*proxies, *groups] if isinstance(item, dict)):
    raise SystemExit("reserved capture proxy name already exists")

mode = unix_get("/configs").get("mode") or config.get("mode")
if mode == "global":
    selected = unix_get("/proxies/GLOBAL").get("now")
    available = {item.get("name") for item in [*proxies, *groups] if isinstance(item, dict)} | {"DIRECT", "REJECT"}
    if not selected or selected not in available:
        raise SystemExit("current GLOBAL selection is not reusable")
    groups = [{"name": default_group, "type": "select", "proxies": [selected]}, *groups]
    rules = [f"MATCH,{default_group}"]
elif mode == "direct":
    rules = ["MATCH,DIRECT"]
elif mode == "rule":
    rules = list(config.get("rules") or [])
else:
    raise SystemExit(f"unsupported Clash mode: {mode}")

config["mode"] = "rule"
config["proxies"] = [{"name": capture_proxy, "type": "http", "server": "127.0.0.1", "port": 2023}, *proxies]
config["proxy-groups"] = groups
config["rules"] = [
    "PROCESS-NAME,wx_video_download,DIRECT",
    f"DOMAIN-SUFFIX,qq.com,{capture_proxy}",
    *rules,
]
print(json.dumps({"payload": yaml.safe_dump(config, allow_unicode=True, sort_keys=False)}))
PY
) || return 1
    printf '%s' "$payload" | clash_put
}

restore_clash_runtime() {
    [ "$(snapshot_value capture_route)" = "mihomo_runtime" ] || return 0
    [ "$(snapshot_value mihomo_runtime_pending)" = "true" ] || return 0
    payload=$("$python_bin" - "$clash_config" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1]).resolve()
if not path.is_file():
    raise SystemExit("original Clash configuration is missing")
print(json.dumps({"path": str(path)}))
PY
) || return 1
    printf '%s' "$payload" | clash_put || return 1
    mode=$(snapshot_value mihomo_mode)
    printf '{"mode":"%s"}' "$mode" | clash_patch || return 1
    [ "$(clash_get /configs | "$python_bin" -c 'import json,sys; print(json.load(sys.stdin).get("mode") or "")')" = "$mode" ] || return 1
    ! clash_capture_ready "$(snapshot_value service)"
}

remove_capture_certificate() {
    cert_name=$(snapshot_value cert_name)
    [ -n "$cert_name" ] || return 0
    installed=$(security find-certificate -a -c "$cert_name" -p "$HOME/Library/Keychains/login.keychain-db" 2>/dev/null || true)
    [ -z "$installed" ] || security delete-certificate -c "$cert_name" "$HOME/Library/Keychains/login.keychain-db" >/dev/null
}

capture_status() {
    clash_ready=false
    service=$(active_service)
    if [ -n "$service" ] && clash_capture_ready "$service" >/dev/null 2>&1; then
        clash_ready=true
    fi
    api_get /api/proxy/status | "$python_bin" -c '
import json, sys
d = (json.load(sys.stdin).get("data") or {})
config = d.get("config") or {}
service = d.get("service") or {}
system = d.get("system_proxy") or {}
listener = service.get("status") == "running"
enabled = bool(config.get("enabled"))
matched = bool(system.get("matched"))
clash = sys.argv[1] == "true"
system_active = bool(config.get("system")) and matched
route = "system" if system_active else "mihomo" if clash else "none"
active = listener and enabled and (system_active or clash)
print("capture_proxy_listener=" + ("running" if listener else "stopped"))
print("capture_backend_enabled=" + ("true" if enabled else "false"))
print("system_proxy_matched=" + ("true" if matched else "false"))
print("capture_route=" + route)
print("capture_proxy=" + ("running" if active else "stopped"))
' "$clash_ready"
}

enable_capture() {
    check_platform
    api_get /api/status >/dev/null || fail "channels_backend_unavailable"
    service=$(active_service)
    [ -n "$service" ] || fail "active_network_service_not_found"
    existing_proxy=false
    for kind in webproxy securewebproxy socksfirewallproxy; do
        [ "$(proxy_field -get$kind "$service" Enabled)" != "Yes" ] || existing_proxy=true
    done
    capture_route=system
    if [ "$existing_proxy" = true ]; then
        if clash_is_system_proxy "$service"; then
            capture_route=mihomo_runtime
        else
            fail "unsupported_existing_proxy: current proxy was not changed; automatic capture routing requires Clash Verge Rev/Mihomo"
        fi
    fi
    snapshot_proxy
    trap 'cleanup_capture >/dev/null 2>&1 || true' EXIT
    echo "capture_route=$capture_route" >>"$proxy_snapshot"
    if [ "$capture_route" = "mihomo_runtime" ]; then
        mode=$(clash_get /configs | "$python_bin" -c 'import json,sys; print(json.load(sys.stdin).get("mode") or "")')
        case "$mode" in global|rule|direct) ;; *) fail "unsupported_mihomo_mode: $mode" ;; esac
        echo "mihomo_mode=$mode" >>"$proxy_snapshot"
    fi
    cert_name="wechat_archive_$(id -u)_$(date -u +%Y%m%dT%H%M%SZ)"
    api_post /api/proxy/certificate/generate "{\"name\":\"$cert_name\",\"valid_years\":1,\"install\":false,\"restart\":false}"
    cert_file="$backend_runtime/certs/$cert_name.pem"
    [ -f "$cert_file" ] || fail "generated_certificate_not_found"
    echo "cert_name=$cert_name" >>"$proxy_snapshot"
    security add-trusted-cert -r trustRoot -k "$HOME/Library/Keychains/login.keychain-db" "$cert_file"
    proxy_json=$("$python_bin" - "$service" "$capture_route" <<'PY'
import json
import sys

service, route = sys.argv[1:]
print(json.dumps({"values": {
    "proxy.enabled": True,
    "proxy.system": route == "system",
    "proxy.defaultInterface": service,
    "proxy.hostname": "127.0.0.1",
    "proxy.port": 2023,
    "proxy.skipInstallRootCert": True,
    "proxy.upstreamProxy": "",
}, "restart": True}))
PY
)
    api_post /api/proxy/config "$proxy_json"
    if [ "$capture_route" = "mihomo_runtime" ]; then
        echo "mihomo_runtime_pending=true" >>"$proxy_snapshot"
        enable_clash_capture || fail "mihomo_capture_route_failed"
    fi
    capture_status | grep -q '^capture_proxy=running$' || fail "channels_capture_route_not_ready"
    trap - EXIT
    echo "capture=enabled"
    echo "capture_route=$capture_route"
    echo "action_required=open_wechat_login_and_requested_content"
}

restore_proxy() {
    [ -f "$proxy_snapshot" ] || return
    [ "$(snapshot_value capture_route)" = "system" ] || return 0
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
    if [ "$(snapshot_value socks_enabled)" = "Yes" ]; then
        networksetup -setsocksfirewallproxy "$service" "$(snapshot_value socks_server)" "$(snapshot_value socks_port)"
        networksetup -setsocksfirewallproxystate "$service" on
    else
        networksetup -setsocksfirewallproxystate "$service" off
    fi
}

cleanup_capture() {
    cleanup_failed=0
    if [ -f "$proxy_snapshot" ] && ! restore_clash_runtime; then
        return 1
    fi
    if api_get /api/status >/dev/null 2>&1; then
        api_post /api/proxy/config '{"values":{"proxy.enabled":false,"proxy.system":false},"restart":true}' || cleanup_failed=1
    else
        cleanup_failed=1
    fi
    [ -f "$proxy_snapshot" ] || return "$cleanup_failed"
    remove_capture_certificate || cleanup_failed=1
    restore_proxy || cleanup_failed=1
    if [ "$cleanup_failed" -eq 0 ]; then
        find "$proxy_snapshot" -type f -delete 2>/dev/null || true
    fi
    return "$cleanup_failed"
}

disable_capture() {
    capture_route=none
    [ ! -f "$proxy_snapshot" ] || capture_route=$(snapshot_value capture_route)
    cleanup_capture || fail "channels_capture_disable_incomplete: local cleanup was attempted; run disable-capture again after the backend recovers"
    echo "capture=disabled"
    if [ "$capture_route" = "system" ]; then
        echo "previous_proxy=restored"
    else
        echo "previous_proxy=unchanged"
    fi
    echo "capture_certificate=removed"
}

status() {
    doctor
    if api_get /api/status >/dev/null 2>&1; then
        api_get /api/status | "$python_bin" -c 'import json,sys; d=json.load(sys.stdin).get("data") or {}; print("channels_api=" + str((d.get("api") or {}).get("status") or "stopped"))'
        capture_status || {
            echo "capture_proxy_listener=unknown"
            echo "capture_backend_enabled=unknown"
            echo "system_proxy_matched=unknown"
            echo "capture_route=unknown"
            echo "capture_proxy=unknown"
        }
    else
        echo "channels_api=stopped"
        echo "capture_proxy_listener=stopped"
        echo "capture_backend_enabled=unknown"
        echo "system_proxy_matched=unknown"
        echo "capture_route=none"
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
