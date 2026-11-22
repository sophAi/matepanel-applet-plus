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
  - 偏好設定「🎨 外觀與顏色」支援調整 **緊湊二合一進度條寬度 (`2～12 px`)**，加寬進度條時自動擴寬方塊、絕不縮小中間溫度數字。
- 支援三段式自訂字形顏色（正常 / 中度負載 / 高溫警戒）與深淺/透明面板底色適應。

---

## 2. Taskwarrior 任務管理、番茄鐘 & 桌面嵌入式月曆 (`TaskwarriorApplet` / `taskwarrior_desktop_widget.py`)

事件驅動（`Gio.FileMonitor` 監聽 `~/.task`，`0.0%` 閒置 CPU）的 MATE 面板 Taskwarrior 任務管理、番茄鐘倒數小程式與桌面嵌入式月曆：
- **雙欄式互動月曆任務看板**：左側 7×6 彩色月曆（含到期任務微縮標題與歷史已完成任務 `✓` 歸檔），右側當日任務與未排程待辦操作清單，支援偏好設定調整月曆字型大小、開關面板 `TASK` 方塊待辦總數顯示與月曆紅頂風格 `TASK` 面板方塊，以及留空任務標題自動填入不可見佔位字元（`\u200b`）。
- **整合番茄鐘專注計時器、自訂倒數計時與提示音效**：
  - 支援自訂專注工作時間（預設 25 分鐘）、中場休息時間（預設 5 分鐘）與循環次數，提供開始、暫停、繼續、重新計時、切換階段、桌面通知與面板大字體倒數圖示自動切換。
  - **自訂倒數計時模式（右鍵選單 → `倒數計時模式...`）**：支援設定「幾小時幾分幾秒」（含 `30秒～1小時` 快選與 0 分鐘短時倒數）或「倒數至指定時刻（幾點幾分幾秒，自動跨日判斷）」，視窗內附 **停止 / 暫停・繼續 / 重新倒數** 按鈕，沿用番茄鐘面板方塊顯示（≥ 1 小時自動顯示 `H:MM:SS`），時間到播放與番茄鐘工作結束相同的提示音效與桌面通知。
  - **三段階段提示音效與音量控制**：專注完成、5 分鐘中場休息結束、全部循環完成皆會播放區分提示音效；偏好設定「🍅 番茄鐘」分頁內建音效開關、音量滑桿（`0%～100%`）與即時試聽按鈕。
- **Taskwarrior 桌面嵌入式互動月曆（`taskwarrior_desktop_widget.py`）**：
  - 無邊框 RGBA 透明視窗平貼於桌布上方、所有一般視窗下方（`DOCK` 視窗層級 + `keep_below` + `stick` + `skip_taskbar/pager`，完整相容 **Marco** 與 **Compiz**＋Caja；點選桌面檔案絕不被壓到桌布後面，且點擊 MATE Panel「顯示桌面」按鈕不會被隱藏），並支援不依賴視窗管理員的直接拖曳移動與右下角／右邊緣／下邊緣自由縮放，與 Taskwarrior Applet／CLI 即時同步。
  - **單一 7×6 月曆整合任務膠囊與單日獨立排版**：
    - 所有待辦與已完成任務直接顯示於日期格內；點擊任務膠囊即可直接編輯／完成／刪除，點擊日期空白處開啟當日清單與快速新增。
    - 支援介面字型（`9～24 pt`）、任務字型（`6～28 pt`）與詳細任務視窗字型（`9～24 pt`）獨立調整、「任務超出日期框時自動縮小字型」與「顯示日期框右上角任務數字」開關，以及留空任務標題自動填入不可見佔位字元（製作純色標記）。
    - **精簡日期框右鍵選單**：可針對**個別日期**獨立設定 **🎨 自訂背景顏色、框線顏色與框線粗細（`0.5～8.0 px`，存於 `date_cell_overrides`）**、切換 **垂直分割成兩列（雙欄顯示）**、放大／縮小該日任務字型、恢復預設樣式與排版、開啟 **「📋 顯示詳細任務內容」獨立視窗**（含前／後一天導覽、完整標題／專案／標籤／時間戳／註解、反白複製與左下角確認清空當日任務），以及附帶警告確認視窗的 **「🗑 清空當日任務」**。
  - **每筆任務獨立自訂顏色**：新增／編輯視窗提供 8 色快速色塊、`Gtk.ColorButton` 任意選色與 `↺ 自動` 重置（獨立儲存於 `~/.config/matepanel-applet-plus/task-colors.json`，不污染 Taskwarrior 資料庫，文字依背景亮度自動切換深淺高對比色）。
  - **日期列 7 天天氣預報與今日 AQI 圓點**：直接共用 `ClockWeatherApplet` 的 `~/.config/matepanel-applet-plus/weather-cache.json`（零額外 API 請求），於日期格上方置中顯示桌面主題天氣圖示、高低溫、Cairo 向量水滴降雨機率與今日 US AQI 圓點。
  - **工作區全螢幕放大 & 一鍵收合小圖示**：
    - 點擊右上角 **`□`／`❐`** 按鈕可一鍵放大至工作區全螢幕（自動扣除 MATE Panel 區域絕不重疊）並可還原原大小。
    - 點擊右上角 **`—`** 按鈕可隱藏主月曆並收合為 `58×58` 可拖曳 RGBA 迷你日曆圖示（附即時待辦數徽章），徹底釋放桌面空間操作檔案；單擊圖示立即還原原本位置與大小。
  - **4 分頁偏好設定、備份還原與自動維護**：
    - 依類型整理為 **🎨 外觀與顏色 / 📅 月曆與排版 / 📋 詳細任務視窗 / ⚙ 系統與備份** 四大分頁，支援開機自動啟動開關（另可由 `TaskwarriorApplet` 偏好設定點擊 **「🖥️ 啟動桌面月曆」** 一鍵啟動）；偏好設定底部另附純偏好參數的 **`📤 匯出設定...`**／**`📥 匯入設定...`** 按鈕（不含任務與單日日期框樣式覆寫）。
    - **單一封包檔匯出／跨電腦匯入**：透過官方 `task export` / `task import` 一次打包任務、自訂顏色與桌面設定（支援合併匯入或覆蓋還原，並自動夾限視窗至目標螢幕範圍）。
    - **每日自動備份與啟動清理**：內建滾動保留最近 **3 天** 的每日自動備份（存於 `~/.config/matepanel-applet-plus/backups/`），以及可設定保留天數（預設 `180` 天，設為 `0` 即關閉）的啟動一次性官方 CLI 過期資料清理。

---

## 3. StarDict 離線詞典、AI 多引擎翻譯與科學計算機 Plus (`DictCalcApplet`)

結合離線多詞典查詢、AI 多引擎翻譯、拼字建議、單字卡匯出與安全科學計算機的全能 MATE 面板小程式：

### 核心功能
- **極速離線 StarDict 查詢 (`sdcv -n -j -e`) 與 HTML 詞典樣式渲染**：
  - 支援自訂 StarDict 詞典根目錄（預設 `/usr/share/stardict/dic`）與一鍵重新掃描，單次查詢 1～8 套詞典僅 ~7 ms、零常駐索引記憶體。
  - **HTML 詞典樣式解析（`sametypesequence=h`，如 `CALD4` 劍橋詞典）**：內建零依賴 `DictHtmlFormatter`，自動將字色、詞性色塊（如 `NOUN` 底色標籤）、粗／斜／底線、上下標、義項編號與例句縮排轉譯為 Pango Markup，並依深淺背景自動調整字色對比，於 **氣泡框** 與 **無焦點結果視窗** 原生彩色呈現；匯出 **HTML 單字卡** 與 **EPUB 3.0 電子書** 時則自動轉譯為嚴格合規 XHTML，完整保留詞典排版與樣式。
  - **三情境獨立詞典管理**：在偏好設定「📚 詞典管理」中，分別針對 **🪟 無焦點查詢視窗**、**💬 氣泡框**、**✂ Scan Selection 劃詞** 獨立設定啟用詞典、拖曳／按鈕順位排序，並可將 **4 套線上翻譯引擎作為虛擬詞典** 混搭排入順位（離線詞典先即時顯示、線上翻譯並行漸進回填）。
- **離線 `aspell` 單字拼寫建議與字元差異上色 (~2.4 ms)**：
  - 輸入拼錯的單一英文單字時，使用 `sdcv -e` 精確比對並自動呼叫 `/usr/bin/aspell` 提示可能正確拼法，同時**停用線上翻譯引擎**避免覆蓋，並自動將第 1 順位建議單字填入面板輸入框（按 `Enter` 即可查詢）。
  - 透過 `difflib.SequenceMatcher` 逐字元比對原字與建議拼法，於氣泡框與精簡結果視窗中以高對比粗體色（自動依底色亮度切換深／亮綠）醒目標示差異字母。
- **多引擎雙向智慧翻譯（零外部套件依賴）**：
  - 智慧分流：單字／短片語優先查離線 StarDict；句子／段落（最高 2,000 字元、支援多行）自動切換翻譯引擎並雙向翻譯（外文 → 繁體中文 / 中文 → 英文），並支援**翻譯結果自動複製到系統剪貼簿（`Ctrl+V` 直接貼上）**。
  - 支援 **Google 翻譯（免 Key）**、**Gemini API Free（Google AI Studio，含 `HTTP 404` 自動探索最新模型自癒）**、**OpenAI 相容端點（Groq / OpenRouter / 本機 Ollama）**、**DeepL API Free/Pro**。
  - 偏好設定「🌐 翻譯引擎」內建 **🔄 載入可用模型下拉選單**、**🔔 立即測試翻譯** 與失敗自動降級備援回 Google 翻譯。
- **自繪無焦點 RGBA 圓角氣泡框 & 無焦點結果視窗**：
  - **純面板乾淨輸入框**：點擊或按全域快捷鍵（如 `XF86Calculator`）即透過 `applet.request_focus()` 取得輸入焦點，聚焦時自動將框線些微加粗至 `2px` 提示焦點狀態（零位移跳動），查詢結果不搶游標；可於偏好設定自訂輸入框寬度 (`60～320 px`)、背景顏色、不透明度 (`0%～100%`) 與字色。
  - **🪟 結果視窗設定分頁**：統一管理氣泡框與無焦點結果視窗——氣泡框精準鎖定目前螢幕邊界（多螢幕不跨螢幕溢出），支援 **置中上方 / 左上角 / 右上角 / 右下角 / 左下角**、水平／垂直偏移微調、**氣泡最大寬度 (`260～1200 px`)**、自訂背景色／字色／不透明度、字型大小、停留秒數、獨立多泡泡／粗體分隔線合併模式，滑鼠移入暫停消失、單擊或按 `Esc` 立即關閉；**無焦點結果視窗**提供獨立的寬度 (`300～1400 px`) 與字型大小 (`8～22 pt`) 設定，並支援視窗左下角 `◣`／右下角 `◢` 拖曳縮放把手自動記憶尺寸。
  - **偏好設定匯出／匯入（全系列四款面板 Applet 皆支援）**：各 Applet 偏好設定底部皆提供 **`📤 匯出設定...`** 與 **`📥 匯入設定...`** 按鈕（`DictCalcApplet` 匯出時可選擇是否包含翻譯 API 金鑰，匯入空金鑰時不覆蓋本機既有金鑰），方便跨機備份還原。
  - **精簡右鍵選單**：提供 **📌 顯示上次查詢結果 (不自動關閉)**、一鍵切換氣泡框／無焦點視窗、立即查詢反白文字、開關自動劃詞與偏好設定。
- **單字與算式語音發聲（獨立「🔊 語音發聲」分頁）**：
  - **雙引擎順位與開關管理**（比照詞典管理，自由勾選啟用與 `▲ 上移`／`▼ 下移` 調整順位，失敗自動降級至下一順位）：
    - **📖 單字發聲**：`本機真人語音庫 (WyabdcRealPeopleTTS)` → `Microsoft Edge Neural TTS` → `Google Translate TTS` → `espeak-ng`
    - **🧮 算式中文語音**：`Microsoft Edge Neural TTS (台灣中文)` → `Google Translate TTS (台灣中文)`（逐位念出數字，如 `1024` →「一零二四」，不含萬千百十）
  - **語音性質自訂與雙聲道上混**：支援 Edge 男／女／溫柔女聲（曉臻 / 雲哲 / 曉雨、Jenny / Guy / Aria）、**語速 (`0.60x～1.80x`)**、**音調 (`-6～+6`)**、**音量 (`0%～100%`)**、本地快取（上限 300 筆）與 `mpv` 強制左右雙聲道上混（解決 PipeWire 單聲道偏左問題）。
  - **右鍵選單整合**：提供 **🔊 單字／結果重複發聲**、**🔊 查詢單字命中時自動發聲** 與 **🧮 自動發聲計算結果** 即時開關。
- **單字紀錄與單字卡匯出（HTML 互動語音 / EPUB 3.0）**：
  - 僅自動紀錄傳統 StarDict 詞典命中的單字與查詢次數（存於 `~/.config/matepanel-applet-plus/vocab-history.json`）。
  - 偏好設定「🗂 單字卡」支援時間／次數篩選、**隨機洗牌排序**、**「📋 貼上清單」批次匯入（支援每行／逗號／分號／Tab 分隔、自動去除編號與重複字、載入 `.txt` 檔，可加入單字庫或僅針對該清單直接匯出不污染歷史紀錄）**，並比照「詞典管理」提供**多詞典勾選、拖曳與按鈕順位排序（`⏫ 置頂`／`▲ 上移`／`▼ 下移`／`⏬ 置底`、`#1/#2` 順位標示、一鍵套用查詢視窗詞典）**，免外部套件依順位匯出 **互動翻卡 HTML** 與 **標準 EPUB 3.0 電子書**（皆完整呈現 HTML 詞典樣式；EPUB 預設「一字一頁」，亦支援「A–Z 字母分章」）。
  - **HTML 單字卡雙模語音發聲與專屬「⚙️ 語音設定」頁面**：匯出時可勾選依語音順位（Edge / 真人庫 / Google）將高品質音檔以 Base64 內嵌至單一 HTML（100% 離線通用，並支援瀏覽器 `Web Speech API` 自動降級）；每張單字卡附 `🔊` 發音鈕（支援 `S` 鍵重播）、翻卡自動發聲與快速語速切換，並提供獨立 **「⚙️ 語音設定」分頁**，可調整發聲引擎模式、音量、語速、音調、瀏覽器各語系人聲角色、重複朗讀次數（1～3 次）與即時試聽（自動儲存於瀏覽器 `localStorage`）。
- **內建安全科學計算機 (`ast` 引擎)**：
  - 支援 `2^10 + sqrt(144)`、`sin(pi/6)`、`=128*3.14`、`ans*2` 等科學算式，預設自動將結果存入系統剪貼簿 (`Ctrl+V` 直接貼上)。
- **滑鼠劃詞 (Scan Selection) 與全域快捷鍵 (`Keybinder 3.0`)**：
  - 可自訂聚焦輸入框與 Scan Selection 快捷鍵（如 `<Super>o`、`<Super>c`），並透過左鍵釋放時間窗防抖，避免在 `xterm + tmux` 使用 `Ctrl+b` 等快捷鍵時誤觸。

---

## 4. 月曆、時鐘與天氣預報 Plus (`ClockWeatherApplet`)

取代 MATE Panel 內建時鐘、月曆與天氣的小程式（零外部套件依賴、精準每分鐘重繪 `0.0%` 閒置 CPU）：
- **桌面主題天氣圖示 + 簡潔時鐘**：自動採用當前桌面 GTK Icon Theme 天氣圖示（晴天、多雲、陰天、霧氣、陣雨、雷雨、降雪等），顯示即時溫度、日期／星期與 `HH:MM`（不顯示秒數），並支援無框線面板、字型大小／不透明度調整與天氣變化提醒氣泡。
- **三合一無焦點 RGBA 彈出氣泡框**：
  - **📅 天氣月曆（Open-Meteo 免 API Key）**：預設城市為 **台北**（內建台灣主要城市快選與全球城市搜尋）；左側 7×6 月曆於未來 7 天嵌入天氣圖示、高低溫、Cairo 向量水滴降雨機率與今日 AQI 色條，右側即時顯示詳細預報、每 3 小時逐時預測與可選世界時鐘。
  - **🛰️ 衛星雲圖**：內建日本氣象廳向日葵九號（Himawari-9）西太平洋動態衛星雲圖（`SND/ETC` 色調強化等圖層），自動疊加高對比金黃色海岸線與本地快取。
  - **✅ 待辦月曆**：與 `TaskwarriorApplet` 共用即時同步模組，於天氣氣泡框內直接檢視、新增、編輯與完成 Taskwarrior 待辦任務。

---

## 系統需求

- MATE Desktop (`gir1.2-matepanelapplet-4.0`, `python3-gi`, `python3-gi-cairo`)
- 系統 Python 3 (`/usr/bin/python3`)
- `DictCalcApplet` 建議套件：`sdcv`、`aspell`、`aspell-en`、`gir1.2-keybinder-3.0`、`mpv`（雙聲道上混與語速／音調濾鏡）、`espeak-ng`（離線語音備援）
  - Edge Neural TTS 自然男／女聲（免費免 Key）：`/usr/bin/python3 -m pip install --user --break-system-packages edge-tts`（未安裝時自動降級至本機真人庫 / Google TTS）
  - 可選離線英文真人發音庫：`/usr/share/WyabdcRealPeopleTTS`
- `TaskwarriorApplet` 需求：`task` (Taskwarrior)、`pulseaudio-utils`（`paplay`，用於番茄鐘提示音效）
- `TempMonitorApplet` GPU 監控需求：NVIDIA 驅動 (`libnvidia-ml.so.1`)

---

## 快速測試與安裝

```bash
# 1. 獨立視窗預覽測試（免 sudo、免重啟 mate-panel）
./install.sh test-dict
./install.sh test-task
./install.sh test-temp
./install.sh test-clock

# 2. 註冊並安裝至系統 MATE Panel (需 sudo)
./install.sh install        # 安裝全部四款 Applets
./install.sh install clock  # 僅安裝「月曆、時鐘與天氣預報 Plus」
./install.sh install dict   # 僅安裝「StarDict 離線詞典與計算機 Plus」
./install.sh install task   # 僅安裝「Taskwarrior 任務管理 Plus」
./install.sh install temp   # 僅安裝「CPU/GPU 溫度監控 Plus」

# 3. 更新程式碼後重新載入 Applet（免 sudo，MATE Panel 會自動重啟）
./install.sh reload         # 重載全部四款 Applets
./install.sh reload clock   # 僅重載「月曆、時鐘與天氣預報 Plus」
./install.sh reload dict    # 僅重載「StarDict 離線詞典與計算機 Plus」
./install.sh reload temp    # 僅重載「CPU/GPU 溫度監控 Plus」
./install.sh reload task    # 僅重載「Taskwarrior 任務管理 Plus」

# 4. 安裝 / 重載 / 移除「Taskwarrior 桌面嵌入式互動月曆」（免 sudo）
./install.sh install-desktop    # 建立開機自啟並立即啟動桌面月曆
./install.sh reload-desktop     # 更新程式碼後重新啟動桌面月曆
./install.sh uninstall-desktop  # 移除開機自啟並停止桌面月曆

# 5. 鎖定 MATE Panel 面板佈局（防止多螢幕或重開機時 Applet 跑位，免 sudo）
./install.sh save-panel-layout     # 儲存目前面板佈局快照並啟用開機延遲自動還原
./install.sh restore-panel-layout  # 立即從已儲存快照還原面板佈局
./install.sh disable-panel-layout  # 停用開機自動還原面板佈局

# 6. 檢查缺少的必要／建議套件（含 edge-tts / mpv / sdcv / task 等）
./install.sh check-deps

# 7. 從系統 MATE Panel 移除註冊
./install.sh uninstall
```
