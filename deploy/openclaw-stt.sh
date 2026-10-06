#!/bin/sh
# OpenClaw 语音转写入口：任意音频格式 -> 16 kHz 单声道 WAV -> whisper.cpp
#
# 为什么要包一层：
#   1. whisper.cpp 只吃 16-bit PCM WAV，浏览器录音是 webm/opus，
#      手机语音条是 ogg/opus 或 m4a；不转码会直接报 "failed to read audio"。
#   2. 本机开着系统代理，这里读的是本地文件，不涉及网络，不需要管代理。
#
# 用法：openclaw-stt.sh <音频文件路径>    （转写文本打到 stdout）
set -eu

WHISPER_CLI="${WHISPER_CLI:-/opt/homebrew/bin/whisper-cli}"
FFMPEG_BIN="${FFMPEG_BIN:-/opt/homebrew/bin/ffmpeg}"
WHISPER_MODEL="${WHISPER_MODEL:-/Users/dingding/models/whisper/ggml-small.bin}"
WHISPER_THREADS="${WHISPER_THREADS:-6}"

if [ "$#" -lt 1 ]; then
  echo "usage: openclaw-stt.sh <audio-file>" >&2
  exit 2
fi

INPUT="$1"
if [ ! -f "$INPUT" ]; then
  echo "input file not found: $INPUT" >&2
  exit 2
fi

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/openclaw-stt.XXXXXX")"
cleanup() { rm -rf "$WORK_DIR"; }
trap cleanup EXIT INT TERM

# -vn 丢掉可能存在的封面图；-ac 1 -ar 16000 是 whisper 的硬性要求
"$FFMPEG_BIN" -hide_banner -loglevel error -y -i "$INPUT" \
  -vn -ac 1 -ar 16000 -c:a pcm_s16le "$WORK_DIR/audio.wav"

# -nt 关掉时间戳，只要纯文本；-l auto 自动判别语言（中文/英文混说也能处理）
"$WHISPER_CLI" -m "$WHISPER_MODEL" -nt -l auto -t "$WHISPER_THREADS" \
  -np -f "$WORK_DIR/audio.wav" 2>/dev/null | tr -d '\r' | sed '/^[[:space:]]*$/d'
