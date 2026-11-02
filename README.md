# 台股當沖 skill（The Day Trader skill 台股版）

11 個台股當沖 Claude skill：掃描上千檔線圖、追蹤新聞和重大訊息、抓準每一次進場和出場，並在動用真錢之前先重播每一種結果。

免費、MIT 授權、不用註冊、不用 API key（只有選用的永豐金下單串接要用你自己的金鑰）。工具是純 Python，只讀免費的公開資料：Yahoo 股市線圖、
證交所 MIS 報價、證交所與櫃買中心的 OpenAPI、公開資訊觀測站和 Google 新聞。

一個 skill 掃上千張線圖找型態，一個追蹤你關注的每一檔的新聞和重大訊息，一個一根一根 K 棒盯著告訴你
什麼時候進場，一個在你進場前就設好停損和停利。最特別的是時光機：動用真錢之前，它用這檔股票自己最近的
5 分 K 把當天剩下的盤重播 10,000 次，再加上過去每一次同樣型態出現的真實結果，告訴你老實的機率。

而且你沒說 yes，什麼都不會成交。這套 skill 不會自己下單：委託單通過風險檢查、你在對話裡針對這一張回覆
yes 之後，由你自己在券商 App 送出；選用永豐金 Shioaji 串接時，是你在 Claude Code 的權限確認按允許才送出。
預設是模擬交易。

這是 [Jakeschincariol/day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)（美股版，MIT）的台股改寫：
台北時間、證交所休市日（含颱風假）、升降單位、漲跌停、整股和零股、手續費和證交稅、處置股、現股當沖資格，
全都照台股的規則重做。

## 安裝

把這段貼給 Claude：

```
https://github.com/eric861129/day-trader-skill-TW

install skill
```

或在 Claude Code 裡當成 plugin 安裝：

```
/plugin marketplace add eric861129/day-trader-skill-TW
/plugin install day-trader-skill-tw@day-trader-skill-tw
```

Claude Code 會幫 plugin 的 skill 加上前綴，所以裝成 plugin 時指令會是 `/day-trader-skill-tw:trader-scanner`
這樣。想用簡短的 `/trader-scanner`，就直接複製資料夾：

```bash
git clone https://github.com/eric861129/day-trader-skill-TW.git
cp -r day-trader-skill-TW/trader-* ~/.claude/skills/
```

只想在某個專案裡用，就把同樣的資料夾複製到那個專案的 `.claude/skills/`。完全沒有 Claude Code？把任何一個
`SKILL.md` 貼在對話最上面，它就會照著做，只是少了 Python 工具，方法還是一樣。

工具需要 Python 3.8 以上，不用 pip 安裝任何東西（只有選用的永豐金串接要 `pip install shioaji`）。Python 3.13 起預設的嚴格憑證檢查會擋掉證交所、櫃買中心和
公開資訊觀測站的網站，工具已經處理好（憑證鏈和網域名稱照樣驗證）。

## 十一個 skill

| 指令 | 工作 | 做什麼 |
| --- | --- | --- |
| `/trader-scanner` | 掃盤 | 掃上市、上櫃和 ETF 上千檔線圖找型態：開盤區間突破、旗形、超賣反彈，處置股自動排除。 |
| `/trader-news` | 新聞 | 追蹤你關注的每一檔的 Yahoo 股市新聞、Google 新聞和公開資訊觀測站重大訊息，標出會影響股價的。 |
| `/trader-entry` | 進場時機 | 一根一根 K 棒盯著一個型態，告訴你什麼時候該進場。 |
| `/trader-exit` | 出場計畫 | 進場前就設好停損和停利，對齊升降單位、檢查漲跌停，也算出成本吃掉多少。 |
| `/trader-timemachine` | 時光機 | 動用真錢之前重播一筆交易的每一種結果：模擬 10,000 次盤，再加上過去每一次同樣型態的真實結果。 |
| `/trader-risk` | 風險管理 | 用你的規則檢查每一張單，超過就擋：單筆和單日虧損（含成本）、當沖資格與標的、處置股、違約交割風險。 |
| `/trader-budget` | 部位管理 | 依你的預算和單筆最大虧損算出要買幾張，手續費和證交稅都算進去。 |
| `/trader-broker` | 委託單 | 把通過的單變成看得懂的委託單，你說 yes、自己在券商 App 送出，或選用永豐金 Shioaji API 送出；預設模擬交易。 |
| `/trader-alerts` | 到價提醒 | 價格碰到停損、停利、漲跌停，或開盤、13:20 出清的時間到了就提醒你。 |
| `/trader-analyst` | 財報分析 | 讀公開資訊觀測站的月營收、季報和法說會，整理出變化。 |
| `/trader-institutional` | 三大法人 | 追蹤外資、投信、自營商的買賣超，找出投信連買、外資連賣。 |

## 怎麼用

所有檔案都放在你專案裡的 `trader/` 資料夾，每個 skill 讀上一個寫的：

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker   （一筆交易）
alerts：停損、停利、13:20 出清                                              （持有中）
news / analyst / institutional                                               （定時看）
```

一個例子，用真實資料：聯電 2303，2026 年 11 月 2 日（星期一）。

1. **`/trader-scanner`** 在 09:45：240 檔圖裡有 232 檔可用，69 檔收盤站上 15 分鐘開盤區間高點。聯電是其中一檔：
   區間 107.5 到 109.5，09:15 那根 5 分 K 收在 110.0，高出 0.46%。處置股東台 4526 自動排除。
2. **`/trader-news`** 在 09:20：聯電比昨收 111.5 跌 1.35%，早上的新聞多在講它前幾天的急跌和法人賣超
   （「投信連7賣」）。往回看一週，10 月 28 日公開資訊觀測站有第三季財務報告的公告。
3. **`/trader-entry`** 一根一根盯著聯電：09:20 說觸發了，09:15 那根收盤第一次站上開盤區間高點，110.0。
4. **`/trader-exit`** 進場前就設好計畫：停損在開盤區間中點 108.5，每股風險 1.5（3 檔）；停利 1R，111.5；
   13:20 前不論輸贏都出場。手續費加當沖證交稅每股約 0.48，等於 0.32R，扣成本後報酬風險比只剩 0.52。
5. **`/trader-timemachine`** 把當天剩下的盤重播 10,000 次，每一根 5 分 K 都抽自聯電前 58 個交易日同一時段的
   K 棒（種子 19，誰都能重跑）：先到停利 43.9%，先到停損 55.7%，13:20 都沒碰到 0.4%。同一條規則在那 58 個真實
   交易日觸發 25 次：停利 7 次、停損 9 次、都沒碰到 9 次。結論是明顯偏向停損：算進成本，停利要先到 66% 才打平。
   這就是先看再動手的意義。那天 09:41 價格就碰到了停損。
6. **`/trader-budget`** 用 30 萬預算、一筆最多虧 3,000：1 張停損出場約虧 NT$1,972（價差 1,500 加手續費和
   證交稅 472），所以是 1 張。不算成本的話會買 2 張，但 2 張停損出場要虧 3,947，超過上限。它寫出一張委託：
   買進聯電 1 張，市價 IOC，現股當沖，停損 108.5，停利 111.5。
7. **`/trader-risk`** 用你的規則檢查：單筆和單日虧損、交易次數、報酬風險比、進場時段、當沖資格和當天的
   當沖標的清單、處置股、現金夠不夠付全額。通過或擋下，並說明原因。
8. **`/trader-broker`** 用白話把委託單給你看，連券商 App 每一欄要填什麼都列出來。你針對這一張回覆 yes
   （「是，買 1 張聯電」），然後自己在券商 App 送出，再把停損和停利設成條件單。
9. **`/trader-alerts`** 在停損、停利和 13:20 提醒你出清還沒賣掉的部位。回放那天，09:41 那根 1 分 K 收在 108.5，
   09:42 就響了停損提醒。

另外三個定時看：**`/trader-analyst`** 說聯電 9 月營收年增 27.22%，第三季 EPS 1.32 元（去年同季 1.20 元），
第二季稅前淨利有 66.9% 來自業外；**`/trader-institutional`** 說 11 月 2 日投信賣超聯電 17,885 張、外資買超 5,060 張；
**`/trader-news`** 盤前先看一遍你關注的股票。

## 工具

全部是標準函式庫 Python。讀資料的工具只用公開的網址，而且會放慢速度，不會一直打。

```bash
python3 trader-scanner/scan.py universe                    # 上市、上櫃、ETF 清單（證交所 ISIN），順便更新休市日
python3 trader-scanner/scan.py daily                       # 開盤前的型態，整個清單
python3 trader-scanner/scan.py orb --at 09:45              # 到目前為止的開盤區間突破
python3 trader-news/news.py --symbols 2303 --hours 12      # 新聞、重大訊息、今天的漲跌
python3 trader-entry/entry.py check 2303 --setup orb       # 等待 / 觸發 / 過期
python3 trader-entry/entry.py watch 2303                   # 一根一根盯到觸發
python3 trader-exit/exit.py plan --stop or-mid --target 1R --write   # -> trader/plan.json
python3 trader-timemachine/timemachine.py run --write      # 10,000 次重播加上歷史
python3 trader-budget/size.py --write                      # -> trader/orders.json
python3 trader-risk/check.py                               # 每一張 PASS / BLOCK
python3 trader-broker/ticket.py show                       # 委託單，READY 或不是
python3 trader-broker/ticket.py send --index 1             # 選用：送到永豐金（預設永豐的模擬環境）
python3 trader-alerts/alerts.py add --from-plan            # 停損、停利、13:20 出清
python3 trader-alerts/alerts.py check                      # 碰到的響一次
python3 trader-analyst/facts.py 2303                       # 月營收、季報、法說會
python3 trader-institutional/inst.py stock 2303            # 三大法人最近 10 天
```

```bash
python3 -m unittest discover -s tests -v
```

測試全部離線跑，用的是從真實資料切下來的固定檔：聯電 2026-11-02 的 1 分和 5 分 K、前 60 個交易日的 5 分 K、
那天 09:45 的 240 檔開盤區間掃描、證交所 MIS 報價、休市日、處置股和當沖標的清單、Yahoo 和 Google 新聞、
公開資訊觀測站的重大訊息、月營收、季報和法說會，以及 10 個交易日的三大法人買賣超。上面例子裡的每一個數字都由
測試重現。

## 注意事項

**不構成投資建議。** 這是讀公開資料、做算術的軟體。它不知道你的狀況，也看不到未來。大部分當沖的人都賠錢。
你可能賠錢。自己做決定，需要建議就去找有執照的專業人士。

**你沒說 yes，什麼都不會成交。** 只有 `/trader-broker` 會產生委託單，而且要三件事都成立：`/trader-risk` 今天
通過了這一張、你在對話中針對這一張明確回覆 yes、你自己在券商 App 送出。預設不連任何券商。選用的永豐金 Shioaji
串接（`"broker": "shioaji"`）讓 `ticket.py send` 送出同一張已同意的委託，這時最後一下是你在 Claude Code 的權限
確認按允許；`send` 和 `cancel` 永遠不要加進允許清單。其他券商目前沒有串接。不做融資、融券、先賣後買、選擇權或期貨，除非你在設定檔打開並且確認。

**先模擬。** `config.example.json` 預設 `"paper": true`，永豐金串接也預設送到永豐的模擬環境；真錢要同時設
`"paper": false`、指令加 `--live`。券商帳號、密碼和憑證不會出現在對話裡，也不會寫進任何檔案：永豐金的金鑰和
憑證只從環境變數讀，`.gitignore` 也排除了 `.env` 和 `*.pfx`。

**現股當沖有規則。** 要先跟券商簽好當沖契約（設定檔的 `day_trade_enabled` 預設是關的），而且只能做當天
證交所和櫃買中心公布的當沖標的；處置股不行。零股不能當沖，今天買的零股最快下一個交易日才能賣。當沖沒沖掉的
部分 T+2 要付全額，付不出來就是違約交割，所以風險檢查會要求現金夠付整筆。當沖賣出的證交稅是 0.15%，
適用到 2027 年 12 月 31 日。

**時光機是模擬，不是預測。** 它用這檔股票最近 60 天的 5 分 K。未來可能發生這 60 天沒發生過的事：財報、處置、
颱風休市、漲跌停鎖死。把它的機率當成現實檢查，不要當成保證。

**停損是計畫，不是保證。** 急殺、跳空或跌停鎖死時，停損單可能成交得比停損價差很多，甚至賣不掉。顯示的最大
虧損是在停損價成交的虧損（含手續費和證交稅）。

**資料是免費的，不是官方 API。** Yahoo 的圖表網址不是正式 API，可能延遲、改版或限制次數。證交所 MIS 是交易所
自己的網頁，但一樣不是給程式用的合約。掃盤和進場用的是 1 分或 5 分 K，不是每一筆成交。重要的事，下單前用
券商的報價再確認一次。

**三大法人是公開統計，不是內線。** 證交所和櫃買中心收盤後公布前一天或當天的買賣超；它描述已經發生的事，
連買幾天也不代表明天會怎樣。

**稅。** 當沖的證交稅、手續費都算在成本裡，但這裡不提供任何稅務建議。

## 檔案

```
trader-scanner/        SKILL.md, scan.py
trader-news/           SKILL.md, news.py
trader-entry/          SKILL.md, entry.py
trader-exit/           SKILL.md, exit.py
trader-timemachine/    SKILL.md, timemachine.py
trader-risk/           SKILL.md, check.py
trader-budget/         SKILL.md, size.py, config.example.json, targets.example.json
trader-broker/         SKILL.md, ticket.py, sinopac.py（選用的永豐金串接）
trader-alerts/         SKILL.md, alerts.py, alerts.example.json
trader-analyst/        SKILL.md, facts.py, metrics.example.json
trader-institutional/  SKILL.md, inst.py
tests/                 unittest 測試與離線固定檔
```

每個資料夾還有一份相同的 `twmarket.py`（台股規則：時間、休市日、升降單位、漲跌停、成本、清單），所以單獨
複製一個資料夾也能用。改了它就執行 `python tests/sync_twmarket.py trader-scanner/twmarket.py` 同步。

MIT。原作 [Jake Schincariol](https://opusjake.ai)（[Jakeschincariol/day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)）；
台股版 [eric861129](https://github.com/eric861129)。
