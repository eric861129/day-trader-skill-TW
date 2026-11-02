---
name: trader-broker
description: >-
  委託單（Order Ticket）。把通過風險檢查的委託變成一張看得懂的委託單，照著就能在
  自己的券商 App 下單：上市或上櫃、代號、買進或賣出、整股或盤中零股、現股當沖或現股、
  市價或限價、ROD／IOC／FOK、幾張幾股，加上停損和停利要設的條件單，以及停損出場
  約虧多少（含手續費和證交稅）。只有 trader-risk 今天通過了這一張、使用者在對話中針對
  這一張明確回覆 yes，才記錄同意；預設由使用者自己在券商 App 送出。設定
  "broker": "shioaji" 時，也能透過永豐金 Shioaji API 把這一張送出（預設送到永豐的模擬
  環境，真錢要設定檔和指令都明講），並把委託狀態和成交同步回來。也會產生 13:20 前出清
  當沖的賣單、記錄成交和現金，並把每一筆的停損虧損記下來，讓 trader-risk 守住單日上限。
  預設模擬交易。
  使用時機：使用者說「下單」「幫我下單」「送出委託」「買進」「平倉」「出清」「賣掉」
  「當沖出場」「記錄成交」「永豐 API」「Shioaji」「同步成交」「刪單」「place the order」
  「buy it」「flatten」「get me out」，或 /trader-risk 通過之後。
argument-hint: "[委託編號]"
---

# trader-broker

委託單是這套 skill 裡唯一通往真實委託的一步，所以規矩最嚴。每一張委託、每一次，都要
先過三關才會進市場：

1. **trader-risk 今天通過了這一張**（用委託內容的指紋比對，改過任何一個欄位都要重新檢查）。
2. **使用者在對話中針對這一張明確回覆 yes**（或「是」「確認」「確定」）。
3. **最後一下由使用者按**：預設（`"broker": "manual"`）是使用者自己在券商 App 或下單軟體
   送出，Claude 不會、也沒辦法替你按下送出。設成 `"broker": "shioaji"` 時，Claude 執行
   `ticket.py send`，Claude Code 會先跳出權限確認，使用者按允許才會送到永豐金。

`ticket.py` 把委託單顯示出來、檢查回覆是不是針對這一張的明確 yes、產生出清賣單，並把
每個決定記在 `trader/state/broker.json`。沒有 yes，就沒有委託。永豐金 API 的部分在
「串接永豐金 Shioaji（選用）」那一節。

`${CLAUDE_SKILL_DIR}` 是這個 skill 的資料夾（這份 SKILL.md 所在的地方）。

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                      # 每一張，READY 或不是
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "是，買 1 張聯電"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status placed --order-no A1234
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --symbol 2303 --side buy --qty 1000 --price 110.0
python3 "${CLAUDE_SKILL_DIR}/ticket.py" flatten --symbol 2303     # 13:20 前出清今天的當沖
python3 "${CLAUDE_SKILL_DIR}/ticket.py" log
```

其他參數：`--json`、`--now "2026-11-02 09:20"`（台北時間）、`--today 2026-11-02`。
股數一律用「股」：1 張是 1000。

## 讀和寫的檔案

| 檔案 | 內容 |
| --- | --- |
| `trader/orders.json` | 提案的委託（trader-budget 寫的；`flatten` 會換成出清賣單） |
| `trader/state/risk.json` | trader-risk 的結果；只有今天的 PASS 才是 READY |
| `trader/state/broker.json` | 每一個 yes、no、送出、沒送出、失敗和成交，含每一筆進場的停損虧損和金額 |
| `trader/portfolio.json` | 現金和持股，`fill` 和 `result --filled-qty` 會更新（含手續費和證交稅） |
| `trader/plan.json` | `flatten` 用的出清時間和跌停價 |
| `trader/config.json` | `paper`、`flat_by`、手續費折扣、`broker`（`manual` 或 `shioaji`）、`broker_name`（只用來顯示） |

## 步驟：進場

1. `ticket.py show`。只有 READY 的委託才拿給使用者看。NOT READY 或 BLOCKED 的回到
   `/trader-risk`。
2. 一次只給一張，問：「要下這一張嗎？回覆 yes 或 no。」不要問「全部都要嗎」。
3. 執行 `ticket.py approve --index N --reply "<使用者的回覆，一字不改>"`。印出「沒有記錄」
   就是那句回覆不算這一張的明確 yes，再問一次。絕不自己把回覆當成 yes。
4. 印出「已記錄你對第 N 張的 yes」之後，請使用者自己在券商 App 照委託單上的欄位送出：
   市場、代號、買賣、整股或零股、交易別、價格、委託條件、數量。真錢帳戶送出後請他告訴你
   委託書號。模擬交易（預設）不要送到真的券商，直接記錄假設的成交。
5. 記錄結果：`ticket.py result --index N --status placed --order-no 委託書號`（成交了就加
   `--filled-qty 1000 --filled-price 110.0`），沒送出用 `--status skipped`，失敗用
   `--status failed --note 原因`。失敗的單不重送：要重新檢查、重新回覆 yes。

### 什麼樣的回覆算 yes

算：`yes`、`y`、`是`、`是，買 1 張聯電`、`確認`、`確定 1000 股`、`yes 108.5`、
`是，停損出場約虧 1,972`。回覆裡的數字要對得上這一張（股數或張數、價格、停損、停利、
停損虧損），代號本身不算數字。

不算：`好`、`ok`、`可以`、`sure`、`下單`、`是嗎？`、`全部都買`、`是，兩張都買`、
`yes to all`、`是，但停損改 108`、`是，等一下`、`如果到 110 再買`、`是 2 張`（這張是 1 張）、
`是，台積電`（這張是聯電）。

## 步驟：持有中，和收盤前出場

- 買進成交後，在券商 App 設條件單（觸價單）：成交價小於或等於停損時市價賣出，大於或
  等於停利時賣出（或直接掛限價停利）。兩張都設的話，一張成交後要刪掉另一張，有的券商有
  「二擇一」條件單。沒有條件單就用 trader-alerts 響鈴，自己賣。
- 條件單成交了，記錄那筆賣出：`ticket.py fill --symbol 2303 --side sell --qty 1000 --price 108.5`。
- 到 `flat_by`（預設 13:20）還沒出場的，一律出清：
  1. 先在券商 App 刪掉還掛著的停損、停利條件單，免得同一批股票賣兩次。
  2. `ticket.py flatten --symbol 2303` 依 `trader/portfolio.json` 裡今天買的整股寫出賣單
     （或 `--qty 1000`，用券商 App 顯示的庫存）。13:25 前是市價 IOC；13:25 到 13:30 是收盤
     集合競價，只收限價 ROD，所以用今天的跌停價掛賣，等於不限價。
  3. `/trader-risk`。只是賣掉手上持股的賣單，不會被每日上限或進場時段擋下。
  4. `ticket.py show`，請使用者針對這一張回覆 yes，再由使用者自己送出。
- 零股不能當沖：今天買的零股最快下一個交易日才能賣，`flatten` 和 `fill` 都會拒絕在當天賣。
- 13:30 收盤後就賣不掉了。當沖沒沖掉的部分 T+2 要付全額，付不出來就是違約交割。

## 範例：聯電 2026-11-02

trader-budget 寫出 1 張，trader-risk 09:20 通過之後，`ticket.py show` 印出：

```
委託 1／1，模擬交易
  買進 聯電 2303（上市），整股 1 張，市價 IOC，現股當沖
  停損 108.5
  停利 111.5
  停損出場約虧 NT$1,972（含手續費和證交稅 NT$472；急跌、跳空或跌停鎖死時可能虧更多）
  計畫進場 110.0（09:20）
  買進成交後，在券商 App 設條件單（觸價單）：
    停損：成交價小於或等於 108.5 時，市價賣出 1 張。沒有條件單就用 trader-alerts 響鈴，自己賣。
    停利：成交價大於或等於 111.5 時，賣出 1 張；或直接掛限價 111.5 賣出。
    兩張都設的話，一張成交後要刪掉另一張，免得重複賣出（有的券商有「二擇一」條件單會自動處理）。
  13:20 前出清：不論輸贏都賣掉，不留倉（到時用 ticket.py flatten 產生賣單）。沒沖掉的部分 T+2 要付全額，付不出來就是違約交割。
  理由：開盤區間突破，聯電 2303：1 張，由單筆最大虧損 NT$3,000 決定（停損出場約虧 NT$1,972，其中手續費和證交稅 NT$472）
  READY：trader-risk 今天通過了這一張，等你回覆 yes
  在券商 App 依序填：
    市場：上市
    代號：2303 聯電
    買賣：買進
    整股／零股：整股
    交易別：現股（今天先買後賣就是現股當沖，要先跟券商簽好當沖契約）
    價格：市價
    委託條件：IOC（立即成交，沒成交的部分取消）
    數量：1 張（1,000 股）
  模擬交易，不要在真的券商送出，回覆 yes 後用 fill 記錄假設成交。
```

使用者回「好」：沒有記錄，再問一次。回「是，買 1 張聯電」：記錄下來，什麼都還沒送出。
模擬帳戶假設 110.0 成交：`result --index 1 --status placed --filled-qty 1000 --filled-price 110.0`，
手續費 NT$156，30 萬的模擬帳戶現金剩 NT$189,844。13:20 執行 `flatten --symbol 2303` 寫出
「賣出 聯電 2303（上市），整股 1 張，市價 IOC，現股當沖」；13:26 再執行就變成「限價 100.5 ROD」
（跌停價，收盤集合競價）；13:31 執行會拒絕：已經收盤。

## 串接永豐金 Shioaji（選用）

`"broker": "shioaji"` 時，`ticket.py send` 把一張已同意的委託送到永豐金。其他步驟不變：
trader-risk 通過、使用者明確回覆 yes，才輪得到 `send`。

### 第一次設定（使用者自己做）

1. 開永豐金證券帳戶，在理財網的 API 管理頁新增 API Key：勾選「交易」和要用的證券帳戶，
   設定到期日，建議限制 IP。Secret Key 只會出現一次，自己存好。
2. 安裝套件：`pip install shioaji`（或 `uv add shioaji`）。只有 broker 的 `send`、`sync`、
   `cancel`、`positions`、`probe` 需要它，其他 skill 照樣只用 Python 標準函式庫。
3. 在自己的終端機設環境變數（不要貼在對話裡，也不要寫進任何檔案或 `trader/config.json`）：
   `SHIOAJI_API_KEY`、`SHIOAJI_SECRET_KEY`（也接受 shioaji 官方 CLI 用的 `SJ_API_KEY`、
   `SJ_SEC_KEY`）。模擬環境只要這兩個。
4. `trader/config.json` 設 `"broker": "shioaji"`，`"paper"` 保持 `true`。
5. `ticket.py probe`：只登入永豐的模擬環境、列出帳戶、查一檔合約，不下任何單。

### 每一張委託

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "是"     # 使用者的 yes
python3 "${CLAUDE_SKILL_DIR}/ticket.py" send --index 1                     # 送到永豐（模擬環境）
python3 "${CLAUDE_SKILL_DIR}/ticket.py" sync                               # 委託狀態和成交
python3 "${CLAUDE_SKILL_DIR}/ticket.py" cancel --index 1 --reply "是"      # 刪掉還沒成交的
python3 "${CLAUDE_SKILL_DIR}/ticket.py" positions                          # 永豐庫存對 portfolio.json
```

`send` 只有這些都成立才會送：

- 設定檔是 `"broker": "shioaji"`。
- trader-risk 今天通過了這一張（指紋相同）。
- 使用者對這一張的 yes 已經記錄，而且不超過 10 分鐘。過期了就重新檢查、再問一次 yes。
- 這一張今天還沒送過。一張只送一次：送出時出錯、結果不明的，也當成已送出，先 `sync`
  確認，絕不重送。
- 現在是 08:30 到 13:30 的交易日（不送預約單）。
- 模擬交易（`"paper": true`）送到永豐的模擬環境。永豐的模擬環境不收零股：零股的假設
  成交用 `result` 記錄。
- 真錢（`"paper": false`）要同時：設定檔是 false、指令加 `--live`、使用者在對話中確認
  這是真的錢，還要有下單憑證的環境變數 `SHIOAJI_CA_PATH`（.pfx 檔）、`SHIOAJI_CA_PASSWD`、
  `SHIOAJI_PERSON_ID`。正式環境之前，使用者要先在永豐的簽署中心簽 API 下單條款，並在
  模擬環境完成登入和下單測試（約 5 分鐘審核）。

Claude Code 的權限設定：`ticket.py send` 和 `ticket.py cancel` 永遠不要加進允許清單
（不要選「不再詢問」）。每次送出都要跳出權限確認，那就是第三關。

送出之後，用 `sync` 把成交記回 `trader/portfolio.json` 和 `trader/state/broker.json`（每一筆
成交只記一次），trader-risk 就會把它算進今天的筆數和單日虧損。停損和停利不會跟著送出：
成交後在永豐的 App 設條件單，或用 trader-alerts 響鈴；到 `flat_by` 一樣用 `flatten` 產生
賣單，再走一次 trader-risk、yes、`send`。

永豐的使用限制：同一個身分證最多 5 個連線、每天登入 1,000 次、下單和查詢每 10 秒 250 次，
超過會暫停一分鐘。每個指令登入一次、用完就登出。

## 不做的事

- 不自己下單，也不假裝已經下單。三關少一關就沒有委託，一個 yes 只算一張，一張只送一次。
- 不在排程或背景任務裡記錄 yes：沒有人在場說 yes。
- 不問、不讀、不存、不複述券商帳號、密碼或憑證。永豐金的金鑰和憑證只從使用者自己設定
  的環境變數讀，不印出來、不寫進任何檔案，錯誤訊息裡也會遮掉。
- 不把 `"paper"` 改成真錢。要用真錢，由使用者自己改設定檔。
- 不保證成交價：市價單和停損都照市場當下給的價格成交，跌停鎖死時可能賣不掉。

## 安全

- 這是教學與工具，不構成投資建議。PASS 只代表通過規則檢查，不是推薦。
- 預設模擬交易（`"paper": true`）。要不要動用真錢，由使用者自己在自己的設定檔決定。
- 這套 skill 不會自己下單。只有 trader-broker 會產生委託單，而且三件事都成立才會：trader-risk 今天通過了這一張單、使用者在對話中針對這一張單明確回覆 yes、使用者自己在券商 App 或下單軟體送出（設定成永豐金 Shioaji 時，是使用者在 Claude Code 的權限確認上按允許）。
- 不做融資、融券、先賣後買、選擇權或期貨，除非設定檔開啟而且使用者確認。
- 券商帳號、密碼和憑證不在對話裡出現，也不寫進任何檔案。
