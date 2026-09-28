#!/bin/zsh
set -euo pipefail
desk_root="$(cd "$(dirname "$0")" && pwd)"
if [[ ! -f "$desk_root/data/access-code" ]]; then
  echo '请先启动控制台，稍后重新打开此文件。'
else
  printf '\n你的私人控制台访问码（不要发到聊天或 GitHub）：\n\n'
  cat "$desk_root/data/access-code"
  printf '\n\n网页：http://127.0.0.1:8765\n'
  open 'http://127.0.0.1:8765'
fi
read -r '?按回车关闭此窗口…'
