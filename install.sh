#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PANEL_APPLETS_DIR="/usr/share/mate-panel/applets"
DBUS_SERVICES_DIR="/usr/share/dbus-1/services"

# 1. Temp Monitor Applet definitions
TEMP_DIR="${REPO_DIR}/applets/temp-monitor"
TEMP_EXEC="${TEMP_DIR}/temp_monitor_applet.py"
TEMP_APPLET_FILE="org.mate.panel.TempMonitorApplet.mate-panel-applet"
TEMP_SERVICE_FILE="org.mate.panel.applet.TempMonitorAppletFactory.service"

# 2. Taskwarrior Applet definitions
TASK_DIR="${REPO_DIR}/applets/taskwarrior"
TASK_EXEC="${TASK_DIR}/taskwarrior_applet.py"
TASK_APPLET_FILE="org.mate.panel.TaskwarriorApplet.mate-panel-applet"
TASK_SERVICE_FILE="org.mate.panel.applet.TaskwarriorAppletFactory.service"

usage() {
    cat <<EOF
用法: $0 [install|uninstall|test|test-temp|test-task] [all|temp|task]

指令:
  install [all|temp|task]    將 Applet 註冊至系統 MATE Panel 與 D-Bus (預設: all，需要 sudo)
  uninstall [all|temp|task]  從系統移除 Applet 註冊檔 (預設: all，需要 sudo)
  test [temp|task]           啟動獨立 GTK 預覽視窗測試 Applet (預設: task，不需 sudo)
  test-temp                  啟動「CPU/GPU 溫度與負載監控 Plus」獨立預覽視窗
  test-task                  啟動「Taskwarrior 任務管理 Plus」獨立預覽視窗
EOF
}

install_one() {
    local dir="$1"
    local exec_py="$2"
    local applet_file="$3"
    local service_file="$4"
    local title="$5"

    chmod +x "${exec_py}"
    local tmp_applet
    local tmp_service
    tmp_applet="$(mktemp)"
    tmp_service="$(mktemp)"
    sed "s|@APPLET_EXEC@|${exec_py}|g" "${dir}/${applet_file}.in" > "${tmp_applet}"
    sed "s|@APPLET_EXEC@|${exec_py}|g" "${dir}/${service_file}.in" > "${tmp_service}"

    echo "正在安裝 [${title}] 至 ${PANEL_APPLETS_DIR} 與 ${DBUS_SERVICES_DIR} ..."
    sudo install -m 644 "${tmp_applet}" "${PANEL_APPLETS_DIR}/${applet_file}"
    sudo install -m 644 "${tmp_service}" "${DBUS_SERVICES_DIR}/${service_file}"
    rm -f "${tmp_applet}" "${tmp_service}"
}

uninstall_one() {
    local exec_py="$1"
    local applet_file="$2"
    local service_file="$3"
    local title="$4"

    echo "正在移除 [${title}] ..."
    sudo rm -f "${PANEL_APPLETS_DIR}/${applet_file}" "${DBUS_SERVICES_DIR}/${service_file}"
    pkill -f "$(basename "${exec_py}")" || true
}

ACTION="${1:-install}"
TARGET="${2:-all}"

case "${ACTION}" in
    install)
        if [[ "${TARGET}" == "all" || "${TARGET}" == "temp" ]]; then
            install_one "${TEMP_DIR}" "${TEMP_EXEC}" "${TEMP_APPLET_FILE}" "${TEMP_SERVICE_FILE}" "CPU/GPU 溫度監控 Plus"
        fi
        if [[ "${TARGET}" == "all" || "${TARGET}" == "task" ]]; then
            install_one "${TASK_DIR}" "${TASK_EXEC}" "${TASK_APPLET_FILE}" "${TASK_SERVICE_FILE}" "Taskwarrior 任務管理 Plus"
        fi
        echo "✅ 安裝完成！"
        echo "👉 現在可以在 MATE 面板空白處按「右鍵」->「加入面板 (Add to Panel...)」，搜尋："
        echo "   • CPU/GPU 溫度監控 Plus"
        echo "   • Taskwarrior 任務管理 Plus"
        ;;
    uninstall)
        if [[ "${TARGET}" == "all" || "${TARGET}" == "temp" ]]; then
            uninstall_one "${TEMP_EXEC}" "${TEMP_APPLET_FILE}" "${TEMP_SERVICE_FILE}" "CPU/GPU 溫度監控 Plus"
        fi
        if [[ "${TARGET}" == "all" || "${TARGET}" == "task" ]]; then
            uninstall_one "${TASK_EXEC}" "${TASK_APPLET_FILE}" "${TASK_SERVICE_FILE}" "Taskwarrior 任務管理 Plus"
        fi
        echo "✅ 移除完成！"
        ;;
    test)
        if [[ "${TARGET}" == "temp" ]]; then
            exec /usr/bin/python3 "${TEMP_EXEC}" --window
        else
            exec /usr/bin/python3 "${TASK_EXEC}" --window
        fi
        ;;
    test-temp)
        exec /usr/bin/python3 "${TEMP_EXEC}" --window
        ;;
    test-task)
        exec /usr/bin/python3 "${TASK_EXEC}" --window
        ;;
    *)
        usage
        exit 1
        ;;
esac
