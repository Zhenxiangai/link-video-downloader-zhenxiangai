#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
worker="$script_dir/wechat_archive.py"
archive_root=${WECHAT_ARCHIVE_ROOT:-"$HOME/Documents/WeChatArchive"}
case "${WECHAT_WORKER_KIND:-channel}" in
    channel)
        label="com.wechatarchive.transcriber"
        work_dir="$archive_root/jobs/channel-transcriber"
        worker_action="watch-channel-transcripts"
        status_action="transcriber-status"
        ;;
    content)
        label="com.wechatarchive.content"
        work_dir="$archive_root/jobs/content-worker"
        worker_action="watch-content"
        status_action="content-worker-status"
        ;;
    *)
        echo "invalid worker kind" >&2
        exit 64
        ;;
esac
plist="$HOME/Library/LaunchAgents/$label.plist"
domain="gui/$(id -u)"
managed_python="${HERMES_HOME:-$HOME/.hermes}/hermes-agent/venv/bin/python"
[ -x "$managed_python" ] || managed_python=python3

resolve_command() {
    configured=$1
    fallback=$2
    candidate=${configured:-$fallback}
    resolved=$(command -v "$candidate" 2>/dev/null || true)
    if [ -z "$resolved" ]; then
        echo "missing command: $candidate" >&2
        exit 69
    fi
    printf '%s\n' "$resolved"
}

plist_add() {
    /usr/libexec/PlistBuddy -c "$1" "$plist"
}

install_worker() {
    python_path=$(resolve_command "${WECHAT_PYTHON:-}" "$managed_python")
    ffmpeg_path=$(resolve_command "${WECHAT_FFMPEG:-}" ffmpeg)
    whisper_path=$(resolve_command "${WECHAT_WHISPER_CLI:-}" whisper-cli)
    model_path=${WECHAT_WHISPER_MODEL:-"$archive_root/models/ggml-small.bin"}

    if [ ! -f "$worker" ]; then
        echo "worker not found: $worker" >&2
        exit 66
    fi
    if [ ! -f "$model_path" ]; then
        echo "Whisper model not found: $model_path" >&2
        exit 66
    fi

    mkdir -p "$HOME/Library/LaunchAgents" "$work_dir"
    launchctl bootout "$domain/$label" 2>/dev/null || true
    if [ -f "$plist" ]; then
        find "$plist" -type f -delete
    fi
    plutil -create xml1 "$plist"
    plist_add "Add :Label string $label"
    plist_add "Add :ProgramArguments array"
    plist_add "Add :ProgramArguments:0 string $python_path"
    plist_add "Add :ProgramArguments:1 string $worker"
    plist_add "Add :ProgramArguments:2 string $worker_action"
    plist_add "Add :ProgramArguments:3 string --interval"
    plist_add "Add :ProgramArguments:4 string 10"
    plist_add "Add :EnvironmentVariables dict"
    plist_add "Add :EnvironmentVariables:WECHAT_ARCHIVE_ENABLED string 1"
    plist_add "Add :EnvironmentVariables:WECHAT_ARCHIVE_ROOT string $archive_root"
    plist_add "Add :EnvironmentVariables:WECHAT_FFMPEG string $ffmpeg_path"
    plist_add "Add :EnvironmentVariables:WECHAT_WHISPER_CLI string $whisper_path"
    plist_add "Add :EnvironmentVariables:WECHAT_WHISPER_MODEL string $model_path"
    if [ -n "${WECHAT_MP_TOKEN_FILE:-}" ]; then
        plist_add "Add :EnvironmentVariables:WECHAT_MP_TOKEN_FILE string $WECHAT_MP_TOKEN_FILE"
    fi
    plist_add "Add :RunAtLoad bool true"
    plist_add "Add :KeepAlive bool true"
    plist_add "Add :ProcessType string Background"
    plist_add "Add :ThrottleInterval integer 10"
    plist_add "Add :StandardOutPath string $work_dir/worker.stdout.log"
    plist_add "Add :StandardErrorPath string $work_dir/worker.stderr.log"
    chmod 644 "$plist"
    plutil -lint "$plist"
    launchctl bootstrap "$domain" "$plist"
    launchctl enable "$domain/$label"
    launchctl kickstart -k "$domain/$label"
    echo "installed: $plist"
    echo "manifest: $work_dir/manifest.json"
}

status_worker() {
    python_path=$(resolve_command "${WECHAT_PYTHON:-}" "$managed_python")
    launchctl print "$domain/$label" >/dev/null
    echo "launchd: running"
    "$python_path" "$worker" "$status_action"
}

uninstall_worker() {
    launchctl bootout "$domain/$label" 2>/dev/null || true
    if [ -f "$plist" ]; then
        find "$plist" -type f -delete
    fi
    echo "uninstalled: $label"
    echo "archive data kept: $archive_root"
}

case "${1:-}" in
    install) install_worker ;;
    status) status_worker ;;
    uninstall) uninstall_worker ;;
    *)
        echo "usage: $0 {install|status|uninstall}" >&2
        exit 64
        ;;
esac
