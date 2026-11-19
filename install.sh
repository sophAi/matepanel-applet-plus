#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPLET_DIR="${REPO_DIR}/applets/temp-monitor"
APPLET_EXEC="${APPLET_DIR}/temp_monitor_applet.py"

PANEL_APPLETS_DIR="/usr/share/mate-panel/applets"
DBUS_SERVICES_DIR="/usr/share/dbus-1/services"

APPLET_FILE="org.mate.panel.TempMonitorApplet.mate-panel-applet"
SERVICE_FILE="org.mate.panel.applet.TempMonitorAppletFactory.service"

usage() {
    cat <<EOF
用法: $0 [install|uninstall|test]

指令:
  install    將 CPU/GPU 溫度監控 Applet 註冊至系統 MATE Panel 與 D-Bus (需要 sudo)
  uninstall  從系統移除 Applet 註冊檔 (需要 sudo)
  test       啟動獨立 GTK 預覽視窗測試 Applet (不需 sudo、不需重啟 mate-panel)
EOF
}

ACTION="${1:-install}"

case "${ACTION}" in
    install)
        chmod +x "${APPLET_EXEC}"
        TMP_APPLET="$(mktemp)"
        TMP_SERVICE="$(mktemp)"
        sed "s|@APPLET_EXEC@|${APPLET_EXEC}|g" "${APPLET_DIR}/${APPLET_FILE}.in" > "${TMP_APPLET}"
        sed "s|@APPLET_EXEC@|${APPLET_EXEC}|g" "${APPLET_DIR}/${SERVICE_FILE}.in" > "${TMP_SERVICE}"

        echo "正在安裝 MATE Panel Applet 設定檔至 ${PANEL_APPLETS_DIR} 與 ${DBUS_SERVICES_DIR} ..."
        sudo install -m 644 "${TMP_APPLET}" "${PANEL_APPLETS_DIR}/${APPLET_FILE}"
        sudo install -m 644 "${TMP_SERVICE}" "${DBUS_SERVICES_DIR}/${SERVICE_FILE}"
        rm -f "${TMP_APPLET}" "${TMP_SERVICE}"

        echo "✅ 安裝完成！"
        echo "👉 現在可以在 MATE 面板空白處按「右鍵」->「加入面板 (Add to Panel...)」，搜尋「CPU/GPU 溫度監控 Plus」加入面板。"
        ;;
    uninstall)
        echo "正在移除 MATE Panel Applet 設定檔..."
        sudo rm -f "${PANEL_APPLETS_DIR}/${APPLET_FILE}" "${DBUS_SERVICES_DIR}/${SERVICE_FILE}"
        pkill -f "temp_monitor_applet.py" || true
        echo "✅ 移除完成！"
        ;;
    test)
        exec /usr/bin/python3 "${APPLET_EXEC}" --window
        ;;
    *)
        usage
        exit 1
        ;;
esac
