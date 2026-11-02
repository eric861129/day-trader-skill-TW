---
name: trader-alerts
description: >-
  到價提醒（Price Alerts，台股）。價格碰到停損、停利、漲停或跌停，或開盤、13:20 出清的時間到了就提醒你。
  一個指令就把 trader-exit 的交易計畫變成四條提醒：停損、停利、13:20 前出清和跌停；也可以自己設到價
  （高於或低於）、比昨收漲跌幾 %、任何時間和 09:00 開盤，每條只響一次，不會一直吵。知道台股的開收盤：
  08:30 起接受委託、09:00 開盤、盤中零股 09:10 第一次撮合、13:25 收盤集合競價、13:30 收盤，也知道證交所
  的休市日和颱風假，可以抓證交所公布的年度休市日。報價用證交所 MIS（上市上櫃一次抓），沒有就用 Yahoo，
  寫進 trader/prices.json 給 trader-budget 和 trader-risk 用。不會下單。使用時機：使用者說「到價通知」
  「停損提醒」「幫我盯停損」「碰到 111.5 告訴我」「開盤提醒」「13:20 提醒我出清」「現在有開盤嗎」
  「幾點收盤」「颱風假有沒有開市」「幫我報價」「聯電現在多少」，或 /trader-exit 寫好計畫之後。
  也接受 "alert me when"、"watch my stop"、"is the market open"、"get me a price"。
argument-hint: "[代號] [價格]"
---

# trader-alerts

到價提醒幫使用者盯數字，不用一直看盤。提醒只是資訊，永遠不是委託：停損響了，這個 skill 不會賣任何東西。
台灣的交易所沒有停損單，停損和停利要靠使用者在券商 App 設的條件單（觸價單），或使用者自己下的賣單。
提醒是告訴使用者發生了什麼、什麼時候該出清。

`${CLAUDE_SKILL_DIR}` 是這個 skill 的資料夾（這份 SKILL.md 所在的地方）。

```bash
python3 "${CLAUDE_SKILL_DIR}/alerts.py" clock                          # 台股現在有沒有開？下一次開盤
python3 "${CLAUDE_SKILL_DIR}/alerts.py" calendar                       # 今年的平日休市日（含颱風假）
python3 "${CLAUDE_SKILL_DIR}/alerts.py" calendar --update 2027         # 證交所公布後，抓 2027 年的休市日
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --from-plan                # 停損、停利、13:20 出清、跌停，取自 trader/plan.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol 2303 --stop 108.5
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol 2303 --target 111.5
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol 0050 --pct 3
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol 2303 --limit-down
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --at 13:20 --label "13:20 前出清" --symbol 2303
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --opening-bell
python3 "${CLAUDE_SKILL_DIR}/alerts.py" check                          # 有響 exit 0，沒有響 exit 1
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote 2303 6488 --write        # 報價 -> trader/prices.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote --holdings --write       # 計畫、部位和觀察清單的代號
python3 "${CLAUDE_SKILL_DIR}/alerts.py" list
python3 "${CLAUDE_SKILL_DIR}/alerts.py" reset 2303-plan-stop
```

`--now "2026-11-02 09:42"` 固定時間（台北時間）；`--offline PATH` 讀存下來的 MIS 報價或 Yahoo K 線
（檔案或資料夾，可以給好幾個），不連網。

## 讀什麼、寫什麼

| 檔案 | 內容 |
| --- | --- |
| `trader/plan.json` | trader-exit 的交易計畫，給 `add --from-plan` |
| `trader/alerts.json` | 提醒規則（這個資料夾的 `alerts.example.json` 是範例） |
| `trader/state/alerts.json` | 哪些規則響過、什麼時候響，所以每條只響一次 |
| `trader/prices.json` | 最新報價，合併寫入，給 trader-budget 和 trader-risk |
| `trader/portfolio.json`、`trader/watchlist.json` | `quote --holdings` 要報的代號 |
| `trader/config.json` | `flat_by`（計畫沒寫時用）和 `extra_closed`（臨時休市，例如颱風假） |
| `trader/state/calendar-YYYY.json` | `calendar --update` 存下的年度休市日 |

## 規則類型

| 類型 | 什麼時候響 | 會不會再響 |
| --- | --- | --- |
| `above` / `target` | 價格大於或等於 `price` | `reset` 之後；或 `"rearm": true` 而且價格退回去之後 |
| `below` / `stop` | 價格小於或等於 `price` | 同上 |
| `pct_move` | 比昨收（或 `ref`）漲跌 `pct`% 以上，可以只看 `direction` up 或 down | 一天最多一次 |
| `limit_up` | 碰到漲停：漲停鎖住時買單排隊也可能買不到 | 一天最多一次 |
| `limit_down` | 碰到跌停：跌停鎖住時賣單可能賣不掉，停損也可能出不去 | 一天最多一次 |
| `time` | 交易日的台北時間過了 `at`（例如 "13:20"） | 每個交易日一次 |
| `opening_bell` | 09:00 開盤了 | 每個交易日一次 |

有 `"date"` 的規則只在那一個台北日期看；`--from-plan` 的規則都帶計畫的日期，所以昨天的停損今天不會響。
休市日（週末、國定假日、颱風假）不響時間提醒和開盤提醒。

每條規則比的都是**最新成交價**（重播時是 `--now` 之前最後一根收完的 K 棒收盤價），不是那根 K 棒的最低價或
最高價。價格在兩次檢查之間碰到又回去，就看不到，所以部位還在的時候要常常檢查。

## 報價

- `mis`（預設）：證交所的 MIS 報價頁 `mis.twse.com.tw/stock/api/getStockInfo.jsp`，所有代號一次請求，上市
  和上櫃都在裡面；不知道是上市還是上櫃的代號，兩邊一起問，只有對的那邊會回答。會帶回昨收、漲停價、跌停價和
  成交時間。這是交易所自己的網頁，但不是正式 API，格式可能會變。
- `yahoo`：`query2.finance.yahoo.com/v8/finance/chart/2303.TW`（上櫃是 `.TWO`），非官方，一次一檔，間隔
  半秒；遇到 429 等一下再試一次，再不行或 5xx 就改用 `query1`。MIS 沒有某一檔的成交價，或 MIS 整個抓不到，
  就自動改用 Yahoo，並且會說一聲。漲跌停價用昨收算 ±10%。
- 免費報價可能延遲。拿來提醒和檢查張數可以，拿來抓下單時機不行；真正成交的是券商的價格。
  `trader/prices.json` 的每一檔都記著報價本身的時間，trader-budget 和 trader-risk 只用 15 分鐘內的。
- 重播看不到未來：`--now` 之後才成交的 MIS 報價不會用，K 線只用 `--now` 之前已經收完的那幾根
  （09:42 看得到 09:41 那根 1 分 K，看不到 09:42 那根）。

## 台股的時間

- 08:30 起接受委託，09:00 開盤集合競價，之後逐筆交易。
- 盤中零股 09:00 起可以委託，09:10 第一次撮合，之後每 5 秒一次。
- 13:25 到 13:30 是收盤集合競價，只收限價 ROD，13:30 收盤。
- 休市日用 twmarket.py 內建的 2026 年證交所休市日，加上兩個颱風假（7/10 巴威、10/23 薔琵），
  再加上 `calendar --update` 存下的年度和設定檔的 `extra_closed`。證交所的清單不會列颱風假：臨時休市請寫進
  `trader/config.json`，例如 `"extra_closed": {"2026-11-03": "颱風停止交易"}`。
- 一律用台北時間（UTC+8，沒有日光節約），不看電腦自己的時區。清單裡沒有的年份會說一聲。

## 步驟

1. trader-exit 寫好計畫之後，馬上跑 `add --from-plan`，把四條提醒回報給使用者：停損、停利、出清時間、跌停。
2. 部位還在的時候一直跑 `check`：用排程或 `/loop`，每一兩分鐘一次。每次只抓今天規則裡的代號，每條只響一次。
   檢查讀的是最新報價，不是即時串流。
3. 響了什麼，一條一行告訴使用者。停損或停利響了：如果使用者在券商 App 設了條件單，先看有沒有成交，成交了用
   trader-broker 的 `ticket.py fill` 記錄；沒設的話，要不要賣由使用者決定。「13:20 前出清」響了：還有部位就用
   trader-broker 的 `ticket.py flatten --symbol 2303` 產生賣單，再經過 trader-risk 和使用者的 yes。
   不要替使用者做決定。

## 範例：聯電 2026-11-02

計畫：聯電 2303 做多，09:20 進場 110.0，停損 108.5，停利 111.5，13:20 前出清，跌停 100.5。

```
$ alerts.py add --from-plan
已加 2303-plan-stop：聯電 2303 停損：到或低於 108.5，只看 2026-11-02
已加 2303-plan-target：聯電 2303 停利：到或高於 111.5，只看 2026-11-02
已加 2303-flat-by：13:20 響：13:20 前出清（聯電 2303），只看 2026-11-02
已加 2303-limit-down：聯電 2303 跌停，只看 2026-11-02
```

用當天的 1 分 K 重播（`check --now ... --offline 2303-2026-11-02-1m.json`）：

```
09:40  沒有警示。今天看 4 條規則，聯電 2303 109.5。
09:41  沒有警示。今天看 4 條規則，聯電 2303 109.0。
09:42  警示 聯電 2303 現在 108.5（09:41 那根 1 分 K 收盤），小於或等於你的停損 108.5（計畫停損：開盤區間中點）。
09:43  沒有警示（停損只響一次）。
13:20  警示 13:20 前出清：現在台北 13:20（設定 13:20），13:25 起是收盤集合競價（只收限價 ROD），13:30 收盤。
       聯電 2303 還有部位的話就平倉：trader-broker 的 ticket.py flatten --symbol 2303 產生賣單，
       再經過 trader-risk 和你的 yes。
13:31  台股沒有開盤（台北 2026-11-02 13:31（一），已收盤）。聯電 2303 110.0。
```

09:41 那根 K 棒 09:42 才收完，所以 09:42 的檢查才看得到 108.5。停損響了以後，trader-alerts 不會賣：使用者在券商
App 設的觸價單如果成交了，就用 `ticket.py fill` 記錄。

收盤後的報價（MIS）：

```
$ alerts.py quote 2303 6488 2330 --write
聯電 2303（上市）  110.0  -1.35%（昨收 111.5）  漲停 122.5／跌停 100.5  13:30:00 成交  [mis]
環球晶 6488（上櫃）  760  +0.53%（昨收 756）  漲停 831／跌停 681  13:30:00 成交  [mis]
台積電 2330（上市）  2,330  -1.69%（昨收 2,370）  漲停 2,605／跌停 2,135  13:30:00 成交  [mis]
```

颱風假那天：

```
$ alerts.py clock --now "2026-10-23 10:00"
台股沒有開盤（台北 2026-10-23 10:00（五），薔琵颱風，台北市停班，全日休市）。
下一次開盤：2026-10-27（二） 09:00。
```

10/23 颱風、週末、10/26 光復節補假，下一次開盤是星期二。

## 規則

- 提醒是關於價格或時間的事實，就用事實的語氣說。
- 不要把報價說成即時的。免費報價可能延遲，也不是正式資料。
- 每加一條規則都回報給使用者：代號、類型、價格、哪一天。

## 不做的事

不下單、不刪單、不改單，不說報價是即時的，也不預測價格會往哪裡走。

## 安全

- 這是教學與工具，不構成投資建議。PASS 只代表通過規則檢查，不是推薦。
- 預設模擬交易（`"paper": true`）。要不要動用真錢，由使用者自己在自己的設定檔決定。
- 這套 skill 不會自己下單。只有 trader-broker 會產生委託單，而且三件事都成立才會：trader-risk 今天通過了這一張單、使用者在對話中針對這一張單明確回覆 yes、使用者自己在券商 App 或下單軟體送出。
- 不做融資、融券、先賣後買、選擇權或期貨，除非設定檔開啟而且使用者確認。
- 券商帳號、密碼和憑證不在對話裡出現，也不寫進任何檔案。
