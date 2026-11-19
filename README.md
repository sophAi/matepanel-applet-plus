# matepanel-applet-plus

Custom MATE Panel Applets collection and development repository.

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
     - **方塊佈局 1（獨立方塊 `separate`）**：並排顯示 `[CPU 41°]` `[GPU 43°]` `[UTL 19%]` `[MEM 29% / 1.2G]` 四顆動態變色方塊（可個別勾選是否顯示 `UTL` 與 `MEM`，顯存支援 `%` 或 `GiB` 單位切換）。
     - **方塊佈局 2（緊湊二合一 `compact`）**：僅佔兩顆方塊寬度 `[CPU 41°]` `[GPU 43°]`，並在 `GPU` 方塊底部內嵌 `GPU-Util`（青藍→警戒紅）與 `Memory-Usage`（紫藍→警戒紅）雙微型進度條。
  2. **模式 B：晶片小圖示 + 文字並排 (`icon_text`)**：
     - 繪製 `[C] 41°C  [G] 43°C 19% 29%`（或 `1.2G`）。
- **左鍵彈出式選單與右鍵偏好設定視窗**：
  - 左鍵點選完整列出 **所有 CPU 監控單元**、**GPU 溫度 / Volatile GPU-Util 文字進度條 / Memory-Usage 顯存進度條 / 風扇轉速 % / 即時功耗 W**，以及 **其他系統感測器（NVMe SSD、主機板、CPU 風扇 RPM）**。
  - **啟用 / 關閉自動監控**：關閉時立即移除 GLib 計時器並釋放 NVML 控制代碼，達成 **0% 背景輪詢消耗**。
  - **調整監控時間頻率**：支援 `1 / 2 / 3 / 5 / 10 / 30 秒` 或於偏好設定自訂秒數。
  - 設定值自動保存於 `~/.config/matepanel-applet-plus/temp-monitor.json`。

### 快速測試與安裝

```bash
# 1. 檢視目前系統感測器與 NVML GPU 指標直讀 JSON 輸出
/usr/bin/python3 applets/temp-monitor/temp_monitor_applet.py --dump

# 2. 開啟獨立 GTK 模擬面板視窗預覽與測試（免 sudo、免重啟 mate-panel）
./install.sh test

# 3. 註冊並安裝至系統 MATE Panel (需 sudo)
./install.sh install

# 4. 從系統 MATE Panel 移除註冊
./install.sh uninstall
```
