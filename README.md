# matepanel-applet-plus

Custom MATE Panel Applets collection and development repository for Linux desktop productivity.

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

## 3. StarDict 離線詞典、AI 多引擎翻譯與科學計算機 Plus (`DictCalcApplet`)

結合離線多詞典查詢、AI 多引擎翻譯、拼字建議、單字卡匯出與安全科學計算機的全能 MATE 面板小程式：

### 核心功能
- **極速離線 StarDict 查詢 (`sdcv -n -j -e`)**：
  - 支援自訂 StarDict 詞典根目錄（預設 `/usr/share/stardict/dic`）與一鍵重新掃描，單次查詢 1～8 套詞典僅 ~7 ms、零常駐索引記憶體。
  - **三情境獨立詞典管理**：在偏好設定「📚 詞典管理」中，分別針對 **🪟 無焦點查詢視窗**、**💬 氣泡框**、**✂ Scan Selection 劃詞** 獨立設定啟用詞典、拖曳／按鈕順位排序，並可將 **4 套線上翻譯引擎作為虛擬詞典** 混搭排入順位（離線詞典先即時顯示、線上翻譯並行漸進回填）。
- **離線 `aspell` 單字拼寫建議 (~2.4 ms)**：
  - 輸入拼錯的單一英文單字時，使用 `sdcv -e` 精確比對並自動呼叫 `/usr/bin/aspell` 提示可能正確拼法，同時**停用線上翻譯引擎**避免覆蓋，並自動將第 1 順位建議單字填入面板輸入框（按 `Enter` 即可查詢）。
- **多引擎雙向智慧翻譯（零外部套件依賴）**：
  - 智慧分流：單字／短片語優先查離線 StarDict；句子／段落（最高 2,000 字元、支援多行）自動切換翻譯引擎並雙向翻譯（外文 → 繁體中文 / 中文 → 英文）。
  - 支援 **Google 翻譯（免 Key）**、**Gemini API Free（Google AI Studio，含 `HTTP 404` 自動探索最新模型自癒）**、**OpenAI 相容端點（Groq / OpenRouter / 本機 Ollama）**、**DeepL API Free/Pro**。
  - 偏好設定「🌐 翻譯引擎」內建 **🔄 載入可用模型下拉選單**、**🔔 立即測試翻譯** 與失敗自動降級備援回 Google 翻譯。
- **自繪無焦點 RGBA 圓角氣泡框 & 無焦點結果視窗**：
  - **純面板乾淨輸入框**：點擊即透過 `applet.request_focus()` 取得輸入焦點，查詢結果不搶游標；可於偏好設定自訂輸入框寬度 (`60～320 px`)、背景顏色、不透明度 (`0%～100%`) 與字色。
  - **氣泡框設定分頁**：精準鎖定目前螢幕邊界（多螢幕不跨螢幕溢出），支援 **置中上方 / 左上角 / 右上角 / 右下角 / 左下角**、水平／垂直偏移微調、自訂背景色／字色／不透明度、字型大小、停留秒數、獨立多泡泡／粗體分隔線合併模式，滑鼠移入暫停消失、單擊或按 `Esc` 立即關閉。
  - **精簡右鍵選單**：提供 **📌 顯示上次查詢結果 (不自動關閉)**、一鍵切換氣泡框／無焦點視窗、立即查詢反白文字、開關自動劃詞與偏好設定。
- **單字紀錄與單字卡匯出（HTML / EPUB 3.0）**：
  - 僅自動紀錄傳統 StarDict 詞典命中的單字與查詢次數（存於 `~/.config/matepanel-applet-plus/vocab-history.json`）。
  - 偏好設定「🗂 單字卡」支援時間／次數篩選、**隨機洗牌排序**、自由勾選來源詞典，免外部套件直接匯出 **互動翻卡 HTML** 與 **標準 EPUB 3.0 電子書**（預設「一字一頁」，亦支援「A–Z 字母分章」）。
- **內建安全科學計算機 (`ast` 引擎)**：
  - 支援 `2^10 + sqrt(144)`、`sin(pi/6)`、`=128*3.14`、`ans*2` 等科學算式，預設自動將結果存入系統剪貼簿 (`Ctrl+V` 直接貼上)。
- **滑鼠劃詞 (Scan Selection) 與全域快捷鍵 (`Keybinder 3.0`)**：
  - 可自訂聚焦輸入框與 Scan Selection 快捷鍵（如 `<Super>o`、`<Super>c`），並透過左鍵釋放時間窗防抖，避免在 `xterm + tmux` 使用 `Ctrl+b` 等快捷鍵時誤觸。

---

## 系統需求

- MATE Desktop (`gir1.2-matepanelapplet-4.0`, `python3-gi`, `python3-gi-cairo`)
- 系統 Python 3 (`/usr/bin/python3`)
- `DictCalcApplet` 建議套件：`sdcv`、`aspell`、`aspell-en`、`gir1.2-keybinder-3.0`
- `TaskwarriorApplet` 需求：`task` (Taskwarrior)
- `TempMonitorApplet` GPU 監控需求：NVIDIA 驅動 (`libnvidia-ml.so.1`)

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

# 3. 更新程式碼後重新載入 Applet（免 sudo，MATE Panel 會自動重啟）
./install.sh reload         # 重載全部三款 Applets
./install.sh reload dict    # 僅重載「StarDict 離線詞典與計算機 Plus」
./install.sh reload temp    # 僅重載「CPU/GPU 溫度監控 Plus」
./install.sh reload task    # 僅重載「Taskwarrior 任務管理 Plus」

# 4. 從系統 MATE Panel 移除註冊
./install.sh uninstall
```
