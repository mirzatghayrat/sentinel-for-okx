#!/bin/zsh
set -euo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
desk_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$desk_root"
if ! command -v uv >/dev/null 2>&1; then
  echo '请先安装 uv：https://docs.astral.sh/uv/getting-started/installation/'
  echo '安装完成后重新打开此文件。'
  read -r '?按回车退出…'
  exit 1
fi
uv sync --frozen
printf '\nSentinel for OKX 启动中。页面：http://127.0.0.1:8765\n'
printf '访问码：另行双击“打开控制台.command”查看。关闭此窗口会停止程序。\n'
exec /usr/bin/caffeinate -i "$desk_root/.venv/bin/python" "$desk_root/run.py" "$@"
