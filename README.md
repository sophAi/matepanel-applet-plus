# matepanel-applet-plus

Custom MATE Panel Applets collection and development repository.

---

## 1. CPU/GPU 溫度與負載監控 Plus (`TempMonitorApplet`)

零外部子行程（Zero-Subprocess）開銷的 MATE 面板硬體溫度與 NVIDIA GPU 負載監控小程式：
- **核心與硬體直讀**：
  - **CPU / 主機板 / NVMe / 風扇**：直接讀取 `/sys/class/hwmon/hwmon*` (`coretemp`, `k10temp`, `zenpower`, `nvme`, `acpitz`, `asus` 等)。
  - **NVIDIA GPU (NVML 直讀)**：透過 Python `ctypes` 直接載入 `libnvidia-ml.so.1`，零子行程開銷同步讀取：
    - **GPU 核心溫度 (`°C`)**
    - **Volatile GPU-Util (`%` 核心使用率 & 記憶體控制器頻寬使用率)**
    - **Memory-Usage (`MiB / Total MiB` 與 `%` 顯存使用量)**
    - **Fan Speed (`%` 風扇轉速)** 與 **Power Usage (`W` 即時功耗)**
  - **AMD / Intel GPU**：自動讀取 `/sys/class/hwmon` 與 `/sys/class/drm` 下的 `amdgpu`、`radeon`、`nouveau` 感測器。
- **面板圖示呈現樣式與 GPU 佈局（可於偏好設定或左鍵選單自由即時切換）**：
  1. **模式 A：動態方塊圖示 (`badge`)**：
     - **方塊佈局 1（獨立方塊 `separate`）**：並排顯示 `[CPU 41°]` `[GPU 43°]` `[UTL 19%]` `[MEM 29% / 1.2G]` 四顆動態變色方塊。
     - **方塊佈局 2（緊湊二合一 `compact`）**：僅佔兩顆方塊寬度 `[CPU 41°]` `[GPU 43°]`，並在 `GPU` 方塊**左邊與右邊分別內嵌 `Volatile GPU-Util` 與 `Memory-Usage` 的垂直雙進度條**。
  2. **模式 B：晶片小圖示 + 文字並排 (`icon_text`)**：
     - 繪製 `[C] 41°C  [G] 43°C 19% 29%`（或 `1.2G`）。
- **三段式自訂字形顏色與底色適應**：
  - 支援分別自訂 **正常狀態 / 中度負載 / 高溫與高負載警戒** 三種狀態的字形顏色，並可切換深色底、淺色底或完全透明無底色，適應任何底色的 MATE 面板。

---

## 2. Taskwarrior 任務管理 Plus (`TaskwarriorApplet`)

事件驅動（Event-Driven）的 MATE 面板 Taskwarrior 待辦任務管理小程式：
- **`Gio.FileMonitor` 檔案監聽同步（0% 閒置輪詢開銷）**：
  - 直接監聽 `~/.task/` 資料目錄，無論在終端機執行 `task add` / `task done` 或在面板選單操作，Applet 皆會在毫秒內自動同步更新。
- **兩種面板顯示樣式（自由切換）**：
  1. **模式 A：精簡任務計數方塊 (`badge`)**：繪製 `[TASK / 待辦數量]` 圓角方塊，若有任何任務正在執行計時 (`task start`)，右上角會亮起翠綠呼吸圓點。
  2. **模式 B：方塊 + 最高緊急度任務標題 (`badge_text`)**：在方塊右側直接顯示目前 `Urgency` 最高的第一筆待辦任務摘要與到期倒數（例如 `[TASK 1] [H] 加強mate-panel功能 (剩 4 天)`）。
- **三段式自訂字形顏色與底色適應**：
  - 支援自訂 **一般待辦 / 高優先級 (`H`) 或即將到期 / 已逾期 (`Overdue`) 或今日到期** 三段字形顏色，以及深色、淺色、完全透明底色與文字描邊陰影。
- **左鍵互動式任務控制中心**：
  - 依 `Urgency` 緊急度排序列出待辦任務，每筆任務展開子選單支援：
    - ✅ **一鍵標記完成 (`task done`)**
    - ▶️ **開始 / 停止執行計時 (`task start` / `stop`)**
    - ⚡ **快速切換優先級 (`H` / `M` / `L` / `無`)**
    - ✏️ **編輯任務視窗**（可修改描述、專案、優先級、標籤、到期日）
    - 📝 **新增任務註解 (`task annotate`)**（既有註解亦會直接顯示於子選單頂部）
    - 🗑 **刪除任務 (`task delete`)**
  - **➕ 快速新增任務視窗**：自動載入歷史用過的專案（`Linux`, `HPC`, `JS`, `Paper`, `MIDI` 等）下拉選單、常用標籤（`+WORK`, `+HOBBY`）快速按鈕與 GTK 日曆挑選器。
  - **🔍 依專案 / 標籤篩選檢視** 與 **📜 最近已完成任務回顧 & 一鍵復原 (`task undo` / `status:pending`)**。

---

## 快速測試與安裝

```bash
# 1. 獨立視窗預覽測試「Taskwarrior 任務管理 Plus」（免 sudo、免重啟 mate-panel）
./install.sh test-task

# 2. 獨立視窗預覽測試「CPU/GPU 溫度與負載監控 Plus」
./install.sh test-temp

# 3. 註冊並安裝全部 Applets 至系統 MATE Panel (需 sudo)
./install.sh install

#    或僅安裝單一 Applet：
./install.sh install task
./install.sh install temp

# 4. 從系統 MATE Panel 移除註冊
./install.sh uninstall
```
