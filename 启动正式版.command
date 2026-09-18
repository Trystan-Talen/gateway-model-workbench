#!/bin/bash
# 双击启动网关与模型评测工作台（正式数据）。
# 正式数据 = 程序默认数据目录（已迁移的安装为项目内 data/），不使用任何预览或测试数据目录。
# 关闭这个终端窗口或按 Ctrl+C 即停止服务。

cd "$(dirname "$0")" || exit 1
PROJECT_DIR="$(pwd -P)"
PORT="${ROUTE_TEST_PORT:-8666}"
URL="http://127.0.0.1:${PORT}/"
PYTHON="${PROJECT_DIR}/.venv/bin/python"

# 清掉可能从外部环境带进来的预览设置，确保读写的是正式数据与系统安全存储。
unset ROUTE_TEST_DATA_DIR ROUTE_TEST_DISABLE_CREDENTIAL_STORE
export ROUTE_TEST_PORT="$PORT"

pause_exit() {
  echo
  read -r -n 1 -s -p "按任意键关闭窗口…"
  echo
  exit "${1:-1}"
}

healthy() {
  curl -fs -m 2 "${URL}health" 2>/dev/null | grep -q '"ok": *true'
}

echo "网关与模型评测工作台 · 正式版"
echo "项目目录：${PROJECT_DIR}"
echo

if [ ! -x "$PYTHON" ]; then
  echo "未找到运行环境 .venv。请先在终端里进入项目目录执行一次："
  echo "  python3.11 -m venv .venv"
  echo "  .venv/bin/python -m pip install -r requirements-dev.lock"
  pause_exit 1
fi

# 端口已被占用：区分“本项目已在运行”和“被别的程序占用”。
EXISTING_PID="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -n 1)"
if [ -n "$EXISTING_PID" ]; then
  # lsof 把中文等非 ASCII 字符输出为 \xHH 转义，还原后才能与项目目录比较。
  EXISTING_DIR="$(printf '%b' "$(lsof -a -p "$EXISTING_PID" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1)")"
  EXISTING_CMD="$(ps -p "$EXISTING_PID" -o command= 2>/dev/null)"
  if [ "$EXISTING_DIR" = "$PROJECT_DIR" ] && [[ "$EXISTING_CMD" == *route_test_ui.py* ]] && healthy; then
    STARTED="$(ps -p "$EXISTING_PID" -o lstart= 2>/dev/null | xargs)"
    echo "工作台已在运行（进程 ${EXISTING_PID}，启动于 ${STARTED}）。"
    echo "如果之后更新过代码，需要重启才会生效；重启会中断正在执行的测试。"
    read -r -n 1 -p "按 R 重启，按其他键直接打开页面：" CHOICE
    echo
    if [[ "$CHOICE" != [Rr] ]]; then
      open "$URL"
      echo "已打开 ${URL}（服务仍由原来的窗口或进程负责）。"
      pause_exit 0
    fi
    echo "正在停止旧服务…"
    kill "$EXISTING_PID" 2>/dev/null
    for _ in $(seq 1 50); do
      lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1 || break
      sleep 0.2
    done
    if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
      echo "旧服务未能在 10 秒内停止，请手动结束进程 ${EXISTING_PID} 后重试。"
      pause_exit 1
    fi
  else
    echo "端口 ${PORT} 已被其他程序占用（进程 ${EXISTING_PID}，目录：${EXISTING_DIR:-未知}）。"
    echo "请关闭该程序，或在终端用 ROUTE_TEST_PORT=其他端口 运行本文件。"
    pause_exit 1
  fi
fi

"$PYTHON" route_test_ui.py &
SERVER_PID=$!
# 关闭窗口、Ctrl+C 或脚本退出时一并停止服务，不留后台进程。
trap 'kill "$SERVER_PID" 2>/dev/null; wait "$SERVER_PID" 2>/dev/null' EXIT
trap 'exit 130' INT TERM HUP

echo "正在启动服务…"
for _ in $(seq 1 150); do
  if healthy; then
    echo "服务已就绪：${URL}"
    echo "关闭此窗口或按 Ctrl+C 即停止服务。"
    echo
    open "$URL"
    wait "$SERVER_PID"
    STATUS=$?
    if [ "$STATUS" -eq 143 ]; then
      echo "服务已被停止（可能是在另一个窗口选择了重启）。这个窗口可以关闭。"
    else
      echo "服务已退出（状态 ${STATUS}）。"
    fi
    pause_exit "$STATUS"
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    wait "$SERVER_PID"
    echo "服务启动失败，请查看上方输出。"
    pause_exit 1
  fi
  sleep 0.2
done

echo "服务 30 秒内没有就绪，请查看上方输出。"
pause_exit 1
