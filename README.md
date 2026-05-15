## 技術規格（Technical Spec）

### Overview（專案概觀）

本專案是 **三菱 MELSEC PLC 輪詢橋接程式**，主要功能如下：

- **PLC 輪詢**：依 `config.yaml` 的 `melsec.tags` 設定定期讀取 PLC。
- **資料落地**：將每次輪詢的結果寫入
  - InfluxDB（時間序列）
  - MongoDB（文件式）
- **Web UI / API**：內建 FastAPI + Uvicorn，固定監聽 `0.0.0.0:7001`，提供狀態查詢、匯入/匯出設定、重載與授權相關操作。
- **執行模式**：可直接以 console 執行，也可打包成單一 exe 後安裝為 Windows Service（pywin32）。

### `config.yaml`（設定檔說明）

`config.yaml` 為整體運行設定，主要區段如下：

- **`app`**
  - **`app_name`**：應用名稱（識別用）
  - **`log_level`**：日誌等級（例如 `INFO`）
  - **`log_file_count`**：保留 log 檔數量上限
- **`influxdb`**
  - **`url` / `token` / `org` / `bucket`**：InfluxDB 連線與寫入目標
  - **`measurement`**：寫入 measurement 名稱
- **`mongodb`**
  - **`host` / `port` / `database` / `collection`**：MongoDB 目標集合
  - **`user` / `password`**：若啟用驗證則填入
- **`melsec`**
  - **`name`**：會寫入資料（例如 Influx tag）的 PLC 名稱
  - **`ip` / `port` / `frame_type`**：PLC 通訊參數
  - **`max_connections`**：同時連線數（同時也是輪詢 thread pool 大小；請勿超過 PLC 允許上限）
  - **`poll_interval`**：輪詢週期（秒）
  - **`connect_retries`** / **`connect_retry_delay_sec`**（可選）：PLC TCP 連線失敗時的重試次數與初始間隔（指数退避上限約 20s）；適用開機後網路尚未就緒（例如 `WinError 10051`）。
  - **`tags`**：欲輪詢/控制的點位集合
    - 每個 tag 典型欄位為 **`access`**（`read`/`write`）、**`device`**（例如 `D12`/`Y0`）、**`datatype`**（例如 `bool`/`uint16`/`float`）

### License / Trial（授權與試用）

本程式支援「正式授權」與「試用模式」：

- **Trial 時間**：預設 **3600 秒（1 小時）**。在試用倒數歸零後，程式會進入停止流程（停止輪詢、斷開服務連線）。
- **判定 Licensed 的條件（程式邏輯）**：
  - `license.crt` 必須存在（憑證檔）
  - 且本機需存在綁定資料（優先 `license.binding`，其次相容舊檔 `license_meta.json`）
  - 並且 `license.crt` 驗證通過、Product ID 必須符合 `PRODUCT_ID=neoedgex_melsec_bridge`，且憑證未過期/未尚未生效
- **綁定方式（硬體/TPM）**：
  - 產生 CSR 時會在本機建立 TPM 綁定資料（`license.binding`），用於後續解密/驗證；若換機器或 TPM 不同，授權會驗證失敗。
- **授權相關檔案位置**：
  - 授權與綁定檔會放在「程式資料夾」（通常就是 **exe 同目錄**）：
    - `license.crt`
    - `license.csr`
    - `license.binding`（或舊版 `license_meta.json`）

> 補充：Web API 會提供 CSR 匯出與授權驗證/上傳等端點（實際路由在 `web.py`），用於產生 `license.csr` 與寫入 `license.crt`。

## NeoEdge Melsec Bridge（Windows Service / 單一 EXE）

本專案可用 **PyInstaller** 打包成 **單一 onefile `.exe`**，並透過 **pywin32** 直接以同一支 `.exe` 安裝/管理 Windows Service。

### 建置（每次改碼後重新打包）

在專案根目錄執行：

```bat
build_exe.bat
```

成功後輸出檔案會在：

- `dist\melsecBridge.exe`

### 設定檔位置（`config.yaml`）

程式啟動時會依序尋找設定檔：

- `C:\config.yaml`
- 找不到才會找 `melsecBridge.exe` 同資料夾內的 `config.yaml`

建議部署時把 `config.yaml` 放在 **exe 同目錄**，例如：

- `C:\platinum\melsecBridge.exe`
- `C:\platinum\config.yaml`

### 靜態檔案資料夾（`static\`）

此專案的 Web UI 需要 `static\`（例如 `static\index.html`、`static\app.js`、`static\style.css`）。

- **開發/直接跑原始碼時**：請確保在「專案根目錄」存在 `static\` 資料夾。
  - 位置：`C:\repo\platinum\static\`
- **打包成單一 exe（onefile）後**：`static\` 會由打包設定（`melsecBridge.spec` 的 `datas=[('static','static')]`）**一起打包進 exe**，部署端通常**不需要另外建立 `static\`**。
  - 只有在你想「部署端自行替換靜態檔」時，才需要放一份 `static\` 在 exe 旁邊並調整程式讀取邏輯（目前程式會從打包內建資源載入）。

### 以 Windows Service 執行（pywin32）

請用「系統管理員」權限開啟 **命令提示字元（cmd.exe）**，並在部署資料夾執行以下指令。

#### 安裝（並設定自動啟動）

```bat
REM 注意：pywin32 的 options（例如 --startup）必須放在 command（install）之前
"C:\platinum\melsecBridge.exe" --startup auto install
```

（可選）指定服務帳號：

```bat
"C:\platinum\melsecBridge.exe" --startup auto --username ".\SomeUser" --password "YourPassword" install
```

#### 啟動 / 停止 / 移除

```bat
"C:\platinum\melsecBridge.exe" start
"C:\platinum\melsecBridge.exe" stop
"C:\platinum\melsecBridge.exe" remove
```

#### Console 除錯模式（不安裝服務）

```bat
"C:\platinum\melsecBridge.exe" debug
```

### 服務名稱

- **顯示名稱（Display name）**：`NeoEdge Melsec Bridge`（在「服務」主控台 `services.msc` 中顯示）
- **識別名稱（Service name）**：`MelsecBridge`（`sc query`、程式安裝／移除指令仍使用此名稱）

若服務已安裝為舊顯示名稱，請先 `"…\melsecBridge.exe" remove` 再重新 `install`，或使用 `--startup … update` 更新設定。

### Troubleshooting（常見問題）

- **`install` 只印 Usage、服務查不到（1060）**：多半是 `--startup` 位置錯誤；請改成 `--startup auto install`。
- **`start` 出現「未及時回應」（1053）**：請確認 `config.yaml` 是否存在於：
  - `C:\config.yaml`，或
  - `melsecBridge.exe` 同目錄下的 `config.yaml`
  - 若設定檔不存在／YAML 無法解析，程式會寫入 `logs\\bootstrap.log`（並且在更早的版本會完全沒有 `app.log`）。
- **除錯建議**：先用 console 模式啟動觀察錯誤訊息：

```bat
"C:\platinum\melsecBridge.exe" debug
```

