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
TASK_DESKTOP_EXEC="${TASK_DIR}/taskwarrior_desktop_widget.py"
TASK_DESKTOP_AUTOSTART="${HOME}/.config/autostart/taskwarrior-desktop-calendar.desktop"
PANEL_LAYOUT_FILE="${HOME}/.config/matepanel-applet-plus/panel-layout.dconf"
PANEL_LAYOUT_AUTOSTART="${HOME}/.config/autostart/matepanel-layout-restore.desktop"
TASK_APPLET_FILE="org.mate.panel.TaskwarriorApplet.mate-panel-applet"
TASK_SERVICE_FILE="org.mate.panel.applet.TaskwarriorAppletFactory.service"

# 3. StarDict Dictionary & Calculator Applet definitions
DICT_DIR="${REPO_DIR}/applets/dict-calc"
DICT_EXEC="${DICT_DIR}/dict_calc_applet.py"
DICT_APPLET_FILE="org.mate.panel.DictCalcApplet.mate-panel-applet"
DICT_SERVICE_FILE="org.mate.panel.applet.DictCalcAppletFactory.service"

# 4. Clock & Weather Calendar Applet definitions
CLOCK_DIR="${REPO_DIR}/applets/clock-weather"
CLOCK_EXEC="${CLOCK_DIR}/clock_weather_applet.py"
CLOCK_APPLET_FILE="org.mate.panel.ClockWeatherApplet.mate-panel-applet"
CLOCK_SERVICE_FILE="org.mate.panel.applet.ClockWeatherAppletFactory.service"

usage() {
    cat <<EOF
用法: $0 [install|uninstall|reload|test|test-temp|test-task|test-dict|test-clock] [all|temp|task|dict|clock]

指令:
  install [all|temp|task|dict|clock]    將 Applet 註冊至系統 MATE Panel 與 D-Bus 並自動重載 (預設: all，需要 sudo)
  uninstall [all|temp|task|dict|clock]  從系統移除 Applet 註冊檔並停止舊行程 (預設: all，需要 sudo)
  reload [all|temp|task|dict|clock]     以 pkill -f 重新載入正在執行的 matepanel-applet-plus Applets (預設: all，免 sudo)
  test [temp|task|dict|clock]           啟動獨立 GTK 預覽視窗測試 Applet (不需 sudo)
  test-temp                             啟動「CPU/GPU 溫度與負載監控 Plus」獨立預覽視窗
  test-task                             啟動「Taskwarrior 任務管理與番茄鐘 Plus」獨立預覽視窗
  test-dict                             啟動「StarDict 離線詞典與計算機 Plus」獨立預覽視窗
  test-clock                            啟動「月曆、時鐘與天氣預報 Plus」獨立預覽視窗
  install-desktop                       安裝並啟動「Taskwarrior 桌面嵌入式月曆」開機自啟 (免 sudo)
  uninstall-desktop                     移除並停止「Taskwarrior 桌面嵌入式月曆」開機自啟 (免 sudo)
  reload-desktop                        重新啟動「Taskwarrior 桌面嵌入式月曆」(免 sudo)
  save-panel-layout                     儲存目前 MATE Panel 佈局快照，並啟用開機延遲自動還原 (防止重開機跑位，免 sudo)
  restore-panel-layout                  立即從已儲存快照還原 MATE Panel 佈局 (免 sudo)
  disable-panel-layout                  停用開機自動還原 MATE Panel 佈局 (免 sudo)
  check-deps [all|temp|task|dict|clock] 檢查並提示缺少的必要／建議系統與語音套件 (免 sudo)
EOF
}

reload_one() {
    local exec_py="$1"
    local title="$2"
    local script_name
    script_name="$(basename "${exec_py}")"
    if pkill -f "${script_name}" 2>/dev/null; then
        echo "🔄 已重載 [${title}] (${script_name})"
    else
        echo "ℹ️  [${title}] (${script_name}) 目前未執行"
    fi
}

check_dependencies() {
    local scope="${1:-all}"
    local missing_apt=()
    local missing_opt=()
    local missing_edge_tts=0

    # Core MATE Panel / GTK3 bindings
    if ! /usr/bin/python3 -c 'import gi; gi.require_version("Gtk","3.0"); gi.require_version("MatePanelApplet","4.0"); import cairo' 2>/dev/null; then
        missing_apt+=("python3-gi" "python3-gi-cairo" "gir1.2-matepanelapplet-4.0")
    fi

    if [[ "${scope}" == "all" || "${scope}" == "task" || "${scope}" == "desktop" ]]; then
        if ! command -v task >/dev/null 2>&1; then
            missing_apt+=("taskwarrior")
        fi
        if ! command -v paplay >/dev/null 2>&1 && ! command -v pw-play >/dev/null 2>&1; then
            missing_apt+=("pulseaudio-utils")
        fi
    fi

    if [[ "${scope}" == "all" || "${scope}" == "dict" ]]; then
        command -v sdcv >/dev/null 2>&1 || missing_apt+=("sdcv")
        command -v aspell >/dev/null 2>&1 || missing_apt+=("aspell" "aspell-en")
        if ! /usr/bin/python3 -c 'import gi; gi.require_version("Keybinder","3.0")' 2>/dev/null; then
            missing_apt+=("gir1.2-keybinder-3.0")
        fi
        if ! command -v mpv >/dev/null 2>&1; then
            missing_apt+=("mpv")
        fi
        if ! command -v espeak-ng >/dev/null 2>&1 && ! command -v espeak >/dev/null 2>&1; then
            missing_opt+=("espeak-ng")
        fi
        if ! command -v edge-tts >/dev/null 2>&1 \
           && [[ ! -x "${HOME}/.local/bin/edge-tts" ]] \
           && [[ ! -x "/usr/local/bin/edge-tts" ]] \
           && [[ ! -x "/opt/anaconda3/bin/edge-tts" ]]; then
            missing_edge_tts=1
        fi
    fi

    if [[ ${#missing_apt[@]} -eq 0 && ${#missing_opt[@]} -eq 0 && ${missing_edge_tts} -eq 0 ]]; then
        echo "✅ 套件依賴檢查通過（所有必要與建議語音套件皆已安裝）！"
        return 0
    fi

    echo ""
    echo "⚠️  偵測到以下缺少的必要／建議套件："
    if [[ ${#missing_apt[@]} -gt 0 ]]; then
        echo "   • 必要系統套件：${missing_apt[*]}"
        echo "     建議安裝指令：sudo apt install -y ${missing_apt[*]}"
    fi
    if [[ ${#missing_opt[@]} -gt 0 ]]; then
        echo "   • 離線語音備援套件：${missing_opt[*]}"
        echo "     建議安裝指令：sudo apt install -y ${missing_opt[*]}"
    fi
    if [[ ${missing_edge_tts} -eq 1 ]]; then
        echo "   • Edge Neural TTS 語音引擎（單字與算式自然男/女聲，免 API Key）：edge-tts"
        echo "     建議安裝指令：/usr/bin/python3 -m pip install --user --break-system-packages edge-tts"
        echo "     （未安裝時仍會自動降級使用本機真人庫 / Google TTS / espeak-ng）"
    fi
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
    pkill -f "$(basename "${exec_py}")" 2>/dev/null || true
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
        if [[ "${TARGET}" == "all" || "${TARGET}" == "dict" ]]; then
            install_one "${DICT_DIR}" "${DICT_EXEC}" "${DICT_APPLET_FILE}" "${DICT_SERVICE_FILE}" "StarDict 離線詞典與計算機 Plus"
        fi
        if [[ "${TARGET}" == "all" || "${TARGET}" == "clock" ]]; then
            install_one "${CLOCK_DIR}" "${CLOCK_EXEC}" "${CLOCK_APPLET_FILE}" "${CLOCK_SERVICE_FILE}" "月曆、時鐘與天氣預報 Plus"
        fi
        echo "✅ 安裝完成！"
        echo "👉 現在可以在 MATE 面板空白處按「右鍵」->「加入面板 (Add to Panel...)」，搜尋："
        echo "   • CPU/GPU 溫度監控 Plus"
        echo "   • Taskwarrior 任務管理 Plus"
        echo "   • StarDict 離線詞典與計算機 Plus"
        echo "   • 月曆、時鐘與天氣預報 Plus"
        check_dependencies "${TARGET}"
        ;;
    uninstall)
        if [[ "${TARGET}" == "all" || "${TARGET}" == "temp" ]]; then
            uninstall_one "${TEMP_EXEC}" "${TEMP_APPLET_FILE}" "${TEMP_SERVICE_FILE}" "CPU/GPU 溫度監控 Plus"
        fi
        if [[ "${TARGET}" == "all" || "${TARGET}" == "task" ]]; then
            uninstall_one "${TASK_EXEC}" "${TASK_APPLET_FILE}" "${TASK_SERVICE_FILE}" "Taskwarrior 任務管理 Plus"
        fi
        if [[ "${TARGET}" == "all" || "${TARGET}" == "dict" ]]; then
            uninstall_one "${DICT_EXEC}" "${DICT_APPLET_FILE}" "${DICT_SERVICE_FILE}" "StarDict 離線詞典與計算機 Plus"
        fi
        if [[ "${TARGET}" == "all" || "${TARGET}" == "clock" ]]; then
            uninstall_one "${CLOCK_EXEC}" "${CLOCK_APPLET_FILE}" "${CLOCK_SERVICE_FILE}" "月曆、時鐘與天氣預報 Plus"
        fi
        echo "✅ 移除完成！"
        ;;
    reload)
        if [[ "${TARGET}" == "all" || "${TARGET}" == "temp" ]]; then
            reload_one "${TEMP_EXEC}" "CPU/GPU 溫度監控 Plus"
        fi
        if [[ "${TARGET}" == "all" || "${TARGET}" == "task" ]]; then
            reload_one "${TASK_EXEC}" "Taskwarrior 任務管理 Plus"
        fi
        if [[ "${TARGET}" == "all" || "${TARGET}" == "dict" ]]; then
            reload_one "${DICT_EXEC}" "StarDict 離線詞典與計算機 Plus"
        fi
        if [[ "${TARGET}" == "all" || "${TARGET}" == "clock" ]]; then
            reload_one "${CLOCK_EXEC}" "月曆、時鐘與天氣預報 Plus"
        fi
        echo "✅ 重載完成（MATE Panel 會自動重啟已加入面板的 Applets）！"
        ;;
    test)
        if [[ "${TARGET}" == "temp" ]]; then
            exec /usr/bin/python3 "${TEMP_EXEC}" --window
        elif [[ "${TARGET}" == "dict" ]]; then
            exec /usr/bin/python3 "${DICT_EXEC}" --window
        elif [[ "${TARGET}" == "clock" ]]; then
            exec /usr/bin/python3 "${CLOCK_EXEC}" --window
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
    test-dict)
        exec /usr/bin/python3 "${DICT_EXEC}" --window
        ;;
    test-clock)
        exec /usr/bin/python3 "${CLOCK_EXEC}" --window
        ;;
    install-desktop)
        chmod +x "${TASK_DESKTOP_EXEC}"
        mkdir -p "$(dirname "${TASK_DESKTOP_AUTOSTART}")"
        cat > "${TASK_DESKTOP_AUTOSTART}" <<EOF
[Desktop Entry]
Type=Application
Name=Taskwarrior Desktop Calendar
Comment=Interactive Taskwarrior monthly calendar embedded on MATE desktop
Exec=/usr/bin/python3 ${TASK_DESKTOP_EXEC}
Terminal=false
X-MATE-Autostart-enabled=true
X-MATE-Autostart-Delay=2
EOF
        pkill -f "taskwarrior_desktop_widget.py" 2>/dev/null || true
        nohup /usr/bin/python3 "${TASK_DESKTOP_EXEC}" >/dev/null 2>&1 &
        echo "✅ 已安裝開機自啟並啟動「Taskwarrior 桌面嵌入式月曆」(${TASK_DESKTOP_AUTOSTART})"
        check_dependencies "desktop"
        ;;
    uninstall-desktop)
        rm -f "${TASK_DESKTOP_AUTOSTART}"
        pkill -f "taskwarrior_desktop_widget.py" 2>/dev/null || true
        echo "✅ 已移除並停止「Taskwarrior 桌面嵌入式月曆」"
        ;;
    reload-desktop)
        pkill -f "taskwarrior_desktop_widget.py" 2>/dev/null || true
        nohup /usr/bin/python3 "${TASK_DESKTOP_EXEC}" >/dev/null 2>&1 &
        echo "🔄 已重新啟動「Taskwarrior 桌面嵌入式月曆」"
        ;;
    save-panel-layout)
        mkdir -p "$(dirname "${PANEL_LAYOUT_FILE}")" "$(dirname "${PANEL_LAYOUT_AUTOSTART}")"
        dconf dump /org/mate/panel/objects/ > "${PANEL_LAYOUT_FILE}"
        cat > "${PANEL_LAYOUT_AUTOSTART}" <<EOF
[Desktop Entry]
Type=Application
Name=MATE Panel Layout Restore
Comment=Restore saved MATE Panel applet positions after startup if drifted
Exec=${REPO_DIR}/install.sh restore-panel-layout --if-drifted
Terminal=false
X-MATE-Autostart-enabled=true
X-MATE-Autostart-Delay=8
EOF
        echo "✅ 已儲存目前 MATE Panel 佈局快照至：${PANEL_LAYOUT_FILE}"
        echo "✅ 已啟用開機延遲自動還原（${PANEL_LAYOUT_AUTOSTART}，延遲 8 秒，偵測到跑位才自動還原並重載面板）"
        echo "💡 建議：在右側各 Applet 右鍵勾選「鎖定於面板」後再執行一次 $0 save-panel-layout 效果最佳！"
        ;;
    restore-panel-layout)
        if [[ ! -f "${PANEL_LAYOUT_FILE}" || ! -s "${PANEL_LAYOUT_FILE}" ]]; then
            echo "❌ 找不到面板佈局快照：${PANEL_LAYOUT_FILE}，請先執行 $0 save-panel-layout"
            exit 1
        fi
        if [[ "${TARGET}" == "--if-drifted" ]]; then
            # Only reload mate-panel if any saved applet's position/edge/toplevel actually drifted
            if /usr/bin/python3 - "${PANEL_LAYOUT_FILE}" <<'PYEOF'
import configparser, subprocess, sys

def parse_layout(text):
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_string(text)
    keys = ("position", "relative-to-edge", "toplevel-id", "panel-right-stick")
    out = {}
    for sec in cp.sections():
        if "/" in sec:
            continue
        out[sec] = {k: cp.get(sec, k, fallback="") for k in keys}
    return out

with open(sys.argv[1], "r", encoding="utf-8") as f:
    saved = parse_layout(f.read())
live_raw = subprocess.check_output(["dconf", "dump", "/org/mate/panel/objects/"], text=True)
live = parse_layout(live_raw)

drifted = False
for sec, skv in saved.items():
    if sec not in live or live[sec] != skv:
        drifted = True
        break
sys.exit(1 if drifted else 0)
PYEOF
            then
                echo "ℹ️  MATE Panel 佈局與快照一致，無需重載"
                exit 0
            fi
        fi
        dconf load /org/mate/panel/objects/ < "${PANEL_LAYOUT_FILE}"
        if command -v mate-panel >/dev/null 2>&1; then
            nohup mate-panel --replace >/dev/null 2>&1 &
            echo "✅ 已從快照還原 MATE Panel 佈局並重新載入面板（${PANEL_LAYOUT_FILE}）"
        else
            echo "✅ 已從快照還原 MATE Panel 佈局（${PANEL_LAYOUT_FILE}）"
        fi
        ;;
    disable-panel-layout)
        rm -f "${PANEL_LAYOUT_AUTOSTART}"
        echo "✅ 已停用開機自動還原 MATE Panel 佈局"
        ;;
    check-deps)
        check_dependencies "${TARGET}"
        ;;
    *)
        usage
        exit 1
        ;;
esac
