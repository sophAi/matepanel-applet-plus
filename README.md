# matepanel-applet-plus

Custom MATE Panel Applets collection and development repository.

## 1. CPU/GPU 溫度監控 Plus (`TempMonitorApplet`)

零外部子行程（Zero-Subprocess）開銷的 MATE 面板硬體溫度監控小程式：
- **核心與硬體直讀**：
  - **CPU / 主機板 / NVMe / 風扇**：直接讀取 `/sys/class/hwmon/hwmon*` (`coretemp`, `k10temp`, `zenpower`, `nvme`, `acpitz`, `asus` 等)。
  - **NVIDIA GPU**：透過 Python `ctypes` 直接載入 `libnvidia-ml.so.1` (NVML) 查詢 GPU 核心溫度，不呼叫 `nvidia-smi` 子行程。
  - **AMD / Intel GPU**：自動讀取 `/sys/class/hwmon` 下的 `amdgpu`、`radeon`、`nouveau` 感測器。
- **兩種面板圖示呈現樣式（可自由即時切換）**：
  1. **雙動態溫度方塊圖示 (`badge`)**：使用 Cairo 即時繪製兩顆獨立方塊圖示，分別內嵌 `CPU` 與 `GPU` 平均溫度，並隨溫度區間自動變換色階（`<55°C` 翠綠、`55~75°C` 琥珀橘、`>=75°C` 警戒紅、關閉時灰階 `OFF`）。
  2. **晶片小圖示 + 溫度文字並排 (`icon_text`)**：繪製 `C` / `G` 晶片圖示搭配即時平均溫度文字（例如 `38°C  40°C`）。
- **點選彈出式選單功能**：
  - 顯示 **所有 CPU 監控單元**（`Package id 0`, `Core 0` ~ `Core N` 及 High/Crit 閾值）與 **所有 GPU 監控單元** 的即時溫度。
  - 選配顯示 **其他系統感測器**（如 NVMe SSD 溫度、主機板 ACPI 溫度、CPU 風扇轉速 RPM）。
  - **啟用 / 關閉自動溫度監控**：關閉時立即移除 GLib 計時器並釋放 NVML 控制代碼，達成 **0% 背景輪詢消耗**。
  - **調整監控時間頻率**：支援 `1 秒`、`2 秒 (預設)`、`3 秒`、`5 秒`、`10 秒`、`30 秒` 或 `自訂秒數...`。
  - 設定值自動保存於 `~/.config/matepanel-applet-plus/temp-monitor.json`。

### 快速測試與安裝

```bash
# 1. 檢視目前系統感測器直讀 JSON 輸出
/usr/bin/python3 applets/temp-monitor/temp_monitor_applet.py --dump

# 2. 開啟獨立 GTK 模擬面板視窗預覽與測試（免 sudo、免重啟 mate-panel）
./install.sh test

# 3. 註冊並安裝至系統 MATE Panel (需 sudo)
./install.sh install

# 4. 從系統 MATE Panel 移除註冊
./install.sh uninstall
```
