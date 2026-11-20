# matepanel-applet-plus

Custom MATE Panel Applets collection and development repository.

---

## 1. CPU/GPU 溫度與負載監控 Plus (`TempMonitorApplet`)

零外部子行程（Zero-Subprocess）開銷的 MATE 面板硬體溫度、系統負載與 NVIDIA GPU 監控小程式：
- **核心與硬體直讀**：
  - **CPU 溫度 / 負載 / RAM**：直讀 `/sys/class/hwmon`、`/proc/stat`、`/proc/loadavg` 與 `/proc/meminfo`。
  - **NVIDIA GPU (NVML 直讀)**：透過 `ctypes` 載入 `libnvidia-ml.so.1`，讀取 GPU 溫度、Volatile GPU-Util (%)、Memory-Usage (MiB/%)、風扇轉速 (%) 與即時功耗 (W)。
- **緊湊二合一對稱方塊佈局**：
  - `CPU` 方塊內嵌左 **CPU Loading %** / 右 **System RAM %** 垂直雙進度條。
  - `GPU` 方塊內嵌左 **GPU-Util %** / 右 **VRAM %** 垂直雙進度條。
- 支援三段式自訂字形顏色（正常 / 中度負載 / 高溫警戒）與深淺/透明面板底色適應。

---

## 2. Taskwarrior 任務管理 & 番茄鐘 Plus (`TaskwarriorApplet`)

事件驅動（`Gio.FileMonitor` 監聽 `~/.task`）的 MATE 面板 Taskwarrior 任務管理與番茄鐘倒數小程式：
- **雙欄式互動月曆任務看板**：左側 7×6 彩色月曆（含到期任務微縮標題與歷史已完成任務 `✓` 歸檔），右側當日任務與未排程待辦操作清單，支援偏好設定調整月曆字型大小。
- **整合番茄鐘專注計時器**：支援 25 分鐘專注 / 5 分鐘休息與自訂循環次數，提供開始、暫停、繼續、重新計時、切換階段、桌面通知與面板大字體倒數圖示自動切換。

---

## 3. StarDict 離線詞典與科學計算機 Plus (`DictCalcApplet`)

結合離線多詞典查詢與安全科學計算機的 MATE 面板小程式：
- **零常駐索引記憶體的離線 StarDict 查詢**：自動掃描 `/usr/share/stardict/dic` 與 `~/.stardict/dic` 的全部 40+ 套詞典，透過背景執行緒呼叫系統原生 C++ `sdcv -n -j` 查詢中/英/日/專業科技詞庫。
- **內建安全科學計算機 (`ast` 引擎)**：
  - 輸入數學公式（如 `2^10 + sqrt(144)`、`sin(pi/6)`、`=128*3.14`、`ans*2`）即時求值。
  - **預設自動將計算結果存入系統剪貼簿 (`CLIPBOARD`)**，可直接按 `Ctrl+V` 貼到任何文字輸入框。
- **雙面板模式**：可自由切換「精簡方塊圖示 (`[DICT / CALC]`)」與「面板內嵌直接輸入框」。
- **滑鼠劃詞查詢 (Scan Selection) 與全域快捷鍵 (`Keybinder 3.0`)**：
  - 預設全域快捷鍵：`Ctrl+Alt+D`（呼叫查詢/計算視窗）、`Ctrl+Alt+S`（立即查詢或計算目前滑鼠反白文字），皆可在偏好設定自訂。
  - 滑鼠中鍵點擊面板圖示可快速開關自動劃詞查詢，並可設定需按住 `Ctrl`/`Alt`/`Shift` 才觸發以避免誤觸。

---

## 快速測試與安裝

```bash
# 1. 獨立視窗預覽測試（免 sudo、免重啟 mate-panel）
./install.sh test-dict
./install.sh test-task
./install.sh test-temp

# 2. 註冊並安裝至系統 MATE Panel (需 sudo)
./install.sh install        # 安裝全部三款 Applets
./install.sh install dict   # 僅安裝「StarDict 離線詞典與計算機 Plus」
./install.sh install task   # 僅安裝「Taskwarrior 任務管理 Plus」
./install.sh install temp   # 僅安裝「CPU/GPU 溫度監控 Plus」

# 3. 從系統 MATE Panel 移除註冊
./install.sh uninstall
```
