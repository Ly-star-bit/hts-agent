# HTS 301 关税批量查询工具

本地离线批量查询**美国对华 Section 301 加征关税**（中国原产商品）的工具。
输入你的 HTS 编码列表，自动输出：基础税率、301 命中情况、适用 9903 子目、加征比例、附加税（ADD/CVD）。

数据全部来自官方原始文件（USITC 全量税率表 + USTR 301 中国清单），**零网络请求、零封 IP 风险**。

提供两种使用方式：**Web 网页版**（推荐）与**命令行版**。

---

## 功能总览（v1.5）

| 功能 | 说明 | 入口 |
|---|---|---|
| 编码查询 | 输入 HTS 编码批量判定 301 加征（可选手动指定原产地） | Web / 命令行 |
| **多原产地** | 原产地选择：**中国**（301 + flip 历史 + FLIP 301 关税）/ **越南**（MFN + FLIP 301 12.5%）/ **其他国家**（MFN 通用轨道 + 按国家查 FLIP 301）；301 仅固定适用于中国 | Web「原产地」下拉 / `--origin` |
| **301 flip 历史** | 同一编码**此前的 301 加征档位**（带生效日期），查询展示"此前 X% → 当前 Y%（2026 现行）"；当前值来自 2026 现行 HTS（含 9903.91.xx 2026 档位） | 查询结果「备注」/ 数据结构 `301 flip历史` |
| **FLIP 301 强迫劳动关税** | **2026-07-24 生效**（USTR Final Action FRN 7-23-26）：60 个被调查经济体全部产品加征，**ANNEX II 豁免清单逐编码生效**——**12.5%**（中国/香港/越南/新加坡/巴西等）、**10%**（加拿大/墨西哥/印度/英国等）、net-of-MFN（EU/台湾合计 10%，日/韩/瑞士合计 12.5%）；**已适用 Section 232 的产品豁免**（不重复征税）；不在 60 名单不适用 | 查询结果「FLIP301」列 / `--origin 国家代码` |
| **301 排除（U.S. note 20）** | 命中 301 清单后再判 USTR 排除。排除分两种：**整号排除**（条目正文就是一个统计号，该号下中国产商品全免）→ 直接判免、加征归零、9903 子目改成排除标目、备注写明报关要填哪个号；**按描述排除**（"…(described in statistical reporting number X)"）→ **绝不自动判免**，只列出条目原文、有效期与 Chapter 99 PDF 页码供人工核对。有效期按**查询当天**重算（9903.88.69/.70 均至 **2026-11-09**，.66/.67/.68 已过期；老标目无日期一律按"有效期未标注"不判免）。8 位查询不判免（排除授予到 10 位），但会点名该子目下哪些统计号是整号排除 | 查询结果「301排除」列（可点开条目原文） |
| **ANNEX II 范围限制（Scope Limitations）** | ANNEX II 里 **1257/2113 条通用豁免带范围限制**（Pharma 700 / Aircraft 541 / Ex 16），**只有落在该范围内的商品才豁免**。这类编码不判为"豁免"，而是标 **`+12.5%(范围存疑)`**：税额按**不豁免**保守计（少收会被 CBP 追补加罚），说明栏给出 FRN 页 137 的官方范围定义（`Ex` 档另附 ANNEX II 该行 Description 原文，因为这一档的范围就写在那一栏里），要求人工核实商品用途后确认 | 查询结果「FLIP301」列（琥珀色虚下划线） |
| **加征开关配置** | `measures_config.json` 可分别**启用/禁用** 中国 301 加征（cn301）与 FLIP 301（flip301），默认全启用；禁用后查询/估算/搜索**不含该加征字段、总税负不叠加**，重新启用恢复 | 编辑 `measures_config.json` |
| **来源追溯** | 每条查询结果的每项税负判定均标注**官方出处**（源文件 + CSV 行号 / PDF 物理页码），点击 📄 弹窗展示来源链，可**直接打开源文件并定位到对应页/行**（PDF 用 `#page=N` 定位）；FLIP 豁免同时标注 ANNEX II **范围限制**（Aircraft/Pharma，需按商品描述核对） | Web「编码查询」结果表「来源」列 / 头部「📄 查看数据来源」 |
| **USITC 官网直达** | 结果表每行 🌐 按钮 / 来源弹窗顶部链接 → 新标签打开 **USITC 官方在线 HTS**（`https://hts.usitc.gov/search?query=<编码>`）直接查该编码最新税率；头部「数据来源」弹窗含官网主入口 | Web「编码查询」结果表 / 弹窗 |
| 税率搜索 | 关键词找品类，按**等效从价税率排序**（最低税率在前） | Web「税率搜索」/ `--search` |
| 成本估算 | 总税负 = 基础税率 + 适用措施 + 附加税（按原产地与配置开关） | Web「成本估算」/ `--estimate` |
| **逐行估算到金额** | 申报清单式表格：**每行自带数量与单位货值**，算出该行货值、综合税负与**预估税费金额**并汇总。单位货值只用于折算从量税——马按头、电池按公斤，全表共用一个单价等于拿电池的公斤价折算马的头价。表内带官方**计量单位**（kg / No. / doz.，来自 HTS `Unit of Quantity`），数量须按该单位填。改数量或单价即自动重算；**合计只累加算得出的行，并注明漏了几行需人工、几行未填** | Web「成本估算」 |
| **清单归类流式进度** | 后端是**三次批量 LLM 调用**（出词 / 精排 / 写报告）+ 一轮本地召回，不是逐行调 AI，所以不做假的"逐行冒出"动画——四格进度条对应真实走到的那一步（只有召回那格的计数逐行动）。**表格在精排结束就推给前端渲染，不再陪着报告多等一轮**：实测 3 行清单表格 5.6s 到、报告 9.2s 到，可用结果提前约 39% | Web「清单归类」/ `POST /api/ai/analyze/stream`（SSE） |
| **申报底单导入** | 上传 xlsx / csv 自动认列（`税则号列` / `申报数量` / `成交单价` 这类非标准表头也认），回填成清单供人工核对后再算；认不出编码的行单独列出不静默丢弃。另提供 `下载模板` | Web「成本估算」→ 导入申报底单 |
| AI 助手 | 自然语言问税（感知原产地）/ 商品归类 / 结果解读 / 清单批量分析 | Web「AI 助手」 |
| 数据变动提醒 | 官方数据更新重建后自动 diff | Web「数据变动」 |
| 历史记录 | 最近查询本地保存，一键重查 | Web「编码查询」底部 |

> 注：301 flip 历史与 FLIP 301 税率/豁免清单为仓库内**官方数据转录**（`data/flip_301.json`、`data/flip301_forced_labor.json`、`data/flip301_exemptions.json`，后者来自 FRN ANNEX II 逐子目提取），官方数据更新后请按"数据更新方法"重建；缺数据的查询显式标注而非静默空值。

---

## 目录结构

```
hts_agent/
├── htsdata.csv                    # 数据源①：USITC 全量税率表（含 9903 子目税率）
├── China Tariffs_2026HTSRev15.pdf # 数据源②：USTR 301 中国清单（8位HTS → 9903 映射）
├── Chapter 99_2026HTSRev18.pdf    # 数据源④：HTS 第 99 章全文（301 排除清单 note 20 正文）
│                                  #   13MB，**不入 git**（判定读的是它的产物
│                                  #   data/sec301_exclusions.json）。缺文件时：
│                                  #   python scripts/check_sources.py --apply --only ch99_pdf
├── app.py                         # ★ Web 网页版（FastAPI），浏览器操作
├── templates/
│   └── index.html                 # 前端页面（Tab 化：查询/搜索/估算/AI/变动）
├── requirements.txt               # 运行依赖（见「安装」）
├── requirements-dev.txt           # 开发/测试依赖（含 pytest）
├── ai_config.json                 # AI 服务配置（默认不启用，见下文；已 gitignore）
├── measures_config.json           # 加征措施开关配置（cn301/flip301，默认全启用，见下文）
├── .env.example                   # 数据库凭据模板 → 复制为 .env 填写（已 gitignore）
├── compare_db_301.py              # MongoDB 产品库 301 加征对账（只读，出差异报表）
├── update_db_301.py               # MongoDB 产品库 301 加征回写（默认计划模式，写前强制备份）
├── scripts/
│   ├── bootstrap.py               # ★ 一条命令装机：拉源 → 建库 → 判定链自检
│   ├── build_db.py                # 解析两份原始数据 → data/sec301_db.json（含版本对比）
│   ├── core.py                    # 核心查询逻辑（Web 与命令行共用）
│   ├── rate.py                    # ★ 税率引擎：税率解析 / 总税负计算 / 关键词搜索
│   ├── ai.py                      # ★ AI 增强层（归类/问税/解读/批量分析，provider 可插拔）
│   ├── cross.py                   # ★ CBP 裁定先例检索（CROSS，联网可选，失败静默降级）
│   ├── cross_sync.py              # 一期：CROSS 元数据镜像同步 → data/cross.db（建议每晚 cron）
│   ├── cross_embed.py             # 二期：裁定 subject 语义索引（qwen3-embedding + sqlite-vec）
│   ├── db_diff.py                 # ★ 数据库版本对比与变动追踪
│   ├── check_sources.py           # 三份官方源文件更新检测/下载（launchd 每日探测，见「数据更新方法」）
│   └── query_301.py               # 命令行版查询工具
├── data/
│   ├── sec301_db.json             # 构建产物（不入 git，需先跑 build_db.py 生成）：合并查询数据库
│   ├── flip_301.json              # 数据源③：301 flip 历史（同一编码此前的加征档位）
│   ├── vietnam_measures.json      # 数据源④：越南适用措施与代表编码说明
│   ├── flip301_forced_labor.json  # 数据源⑤：FLIP 301 强迫劳动关税（60 经济体税率表 + 豁免，2026-07-24 生效）
│   ├── flip301_exemptions.json    # 数据源⑥：FLIP 301 ANNEX II 豁免编码清单（通用 2113 + 按经济体）
│   ├── sec301_exclusions.json     # 数据源⑦：301 排除清单（U.S. note 20，由 Chapter 99 PDF 提取）
│   ├── .db_fingerprint.json       # 构建产物（不入 git）：上次构建的关键字段快照
│   └── .db_changes.json           # 构建产物（不入 git）：最近一次构建的变动清单
├── tests/                         # 单元测试与 API 集成测试
│   ├── test_rate.py               # 税率引擎测试
│   ├── test_ai.py                 # AI 层测试（假 Provider）
│   ├── test_app.py                # Web API 集成测试
│   ├── test_cross.py              # CROSS 裁定检索测试（网络全程 mock，不联网）
│   ├── test_check_sources.py      # 源文件更新检测测试（三家服务器 mock，不联网）
│   └── test_measures.py           # 多措施测试：301 flip/越南轨道/FLIP 301/措施开关配置/中国回归
├── .cache/cross/                  # CROSS 查询缓存（不入 git，可随时删除）
└── output/                        # 命令行查询结果输出目录（不入 git）
```

---

## 安装

推荐用 [uv](https://docs.astral.sh/uv/)（也可用普通 venv + pip，把 `uv pip` 换成 `pip` 即可）：

```bash
uv venv --python 3.12          # 创建 .venv
uv pip install -r requirements.txt
```

开发/跑测试再装一份：`uv pip install -r requirements-dev.txt`

**然后一条命令装机**——拉齐官方源、建库、跑判定链自检：

```bash
.venv/bin/python scripts/bootstrap.py
```

它会逐步检查并在缺什么时自己补：依赖 → 四份官方源（缺的自动下载，其中
Chapter 99 PDF 有 13MB 且不随仓库分发）→ 构建数据库 → 用三条真实编码跑
端到端自检（从价税 + 301 / 从量税折算两个方向 / 逐行估算含计量单位），
最后报出当前 301 排除的到期日。任一步失败都会点名是哪一份、该怎么补。

```bash
.venv/bin/python scripts/bootstrap.py --check       # 只体检，不改任何东西
.venv/bin/python scripts/bootstrap.py --skip-deps   # 依赖已装好，只补数据
```

只想重建数据库（官方数据更新后）：`.venv/bin/python scripts/build_db.py`

之后所有命令都用 `.venv/bin/python`（或先 `source .venv/bin/activate`）。

---

## Web 网页版（推荐）

```bash
.venv/bin/python app.py
```

服务只监听 `127.0.0.1:5000`，**不会自动打开浏览器**，需自行访问。

浏览器打开 **http://127.0.0.1:5000** 即可使用：

- **编码查询**：粘贴/上传 HTS 编码 → 表格展示（命中红色 / 未命中绿色）→ 导出 Excel/CSV
- **税率搜索**：输入英文关键词或编码（如 `lithium battery`、`apparel`、`8507`）→
  按**等效从价税率升序**返回候选子目，找"某个品类税率最低的编码"；
  填写**单位货值**后可折算从量税并给出总税负估算
- **成本估算**：输入编码（或导入底单）→ 逐行填数量与单位货值 → 输出 基础等效从价 / 301 加征 /
  综合税负 / **该行预估税费金额** 与合计
- **AI 助手**：自然语言问税（"锂电池出口美国要交多少税？"）、商品归类（"帮我归类：便携式蓝牙音箱"）、
  上传清单批量分析（每行一个品名）
- **数据变动**：最近一次数据更新后的变动清单（新增/删除/税率变化/301 变化）
- 附赠 FastAPI 交互式 API 文档：**http://127.0.0.1:5000/docs**

## 命令行版

```bash
# 1. 构建数据库（首次或数据更新后执行）
python scripts/build_db.py

# 2a. 查询 Excel/CSV/文本文件中的 HTS 列表（自动识别 HTS 列）
python scripts/query_301.py -i 我的商品.xlsx -o output/结果.xlsx

# 2b. 或命令行直接传编码
python scripts/query_301.py -c "8703.80.00, 8507.60.00, 2931.90.9010"

# 2c. 或交互模式：运行后直接粘贴编码，空行结束
python scripts/query_301.py

# 2d. 指定原产地（CN 中国默认 / VN 越南——越南不叠加 301）
python scripts/query_301.py -c "8507.60.00" --origin VN

# ---- v1.2 新增 ----
# 3. 税率搜索：找某品类税率最低的编码（按等效从价升序）
python scripts/query_301.py --search "lithium battery" --top 20

# 4. 成本估算：总税负 = 基础 + 适用措施 + 附加税（--unit-value 折算从量税，--origin 选原产地）
python scripts/query_301.py --estimate -c "8507.60.00, 0101.21.00" --unit-value 10 --origin CN

# ---- CBP 裁定先例（需联网，可选）----
# 5. 查 CBP 实际把同类商品判给了什么编码
python scripts/cross.py "lithium ion battery" --codes 8507.60.00,8506.50.00
```

---

## CBP 裁定先例（`scripts/cross.py` + 归类对比页）

本地税则库只有品名文本，决定归类的**章注/类注/GRI 都不在其中**——这是本工具的
结构性边界（每张归类卡片底下那句免责说的就是这件事）。[CROSS](https://rulings.cbp.gov)
是 CBP 自己的裁定库（22 万条、每日增量），写着海关**实际**把什么货判给了什么编码，
往往能直接跨过论证给出答案。围绕它建了一套**三层先例检索**，各层职责不同、可独立降级：

```
   问题形态                          用哪一层                              数据/成本
─────────────────────────────────────────────────────────────────────────────
① 已知编码，要它的全部先例    一期 本地镜像（SQLite 反查）        全量元数据，离线毫秒级
   "8506.50.00 历来判过几条？"   code_precedents() → 精确计数        每晚 launchd 全量刷

② 模糊中文描述，要找相关裁定  二期 语义召回（向量检索）          subject+官方品名嵌入
   "带 LED 的毛绒玩具算哪类？"   semantic_precedents() → 语义近邻    qwen3-embedding:8b 本地

③ 召回到候选，要精读确认      三期 按需深读（AI 读正文）        只拉看过的几条正文
   "这条的货物细节真和我的像吗" deepread_precedents() → 逐字摘录    ollama，用户点击才跑
```

**为什么分三层、而不是一个大向量库**：① 编码反查要的是"全部"和"精确计数"，
向量的 top-k 天生答不了；② 语义召回把 22 万条缩到十几条，③ 这十几条 AI 一次读完，
**不需要预先给全库正文做向量**——向量检索是"读不完才用相似度近似"的妥协，十几条不
需要妥协。于是全量成本只花在零成本的元数据+官方品名上，大模型/正文只花在用户真点开
的那几条。任一层不可用（镜像未建/ollama 离线/CROSS 故障）都静默降级到下一个可用层，
本地税则查询始终是主链路。

Web 入口：「搜索与归类」→ 勾选候选 → 归类对比弹窗底部「CBP 裁定先例」。
打开即自动展示本地镜像的完整反查（①，离线毫秒级）；中文描述可「语义检索」（②）；
召回后点「🔍 深读正文」让 AI 读正文并逐字摘录 CBP 原文（③）。
另有「在线检索」直连 rulings.cbp.gov 作兜底。各块失败只影响自身，本地结果不受影响。
**点裁定号在站内看正文**（`/api/cross/text/{裁定号}`）：正文由服务端从 CBP 拉取并永久缓存
（与深读同一份），按 FACTS / ISSUE / LAW AND ANALYSIS / HOLDING 切段、HOLDING 高亮——
rulings.cbp.gov 是美国政府站，境内直连经常打不开，「官网 ↗」只作备用入口。

**验收（2026-09，全量 221,340 条索引）**：7 组中文查询语义召回 6 组干净命中品目，
「带 LED 灯毛绒玩具」直接捞出 8543+9405+9503 三码并存的一物多号先例。已知弱点：
针织 vs 梭织这类技术属性向量捕捉不到（「羊毛大衣」会返回 6102 针织而非 6201 梭织）——
正因如此才需要第三层深读读正文的 knitted/woven 字样来区分。

### 本地镜像（`scripts/cross_sync.py`，一期）

```bash
python scripts/cross_sync.py     # 全量同步 → data/cross.db（首次 ~5 分钟，69MB）
                                 # 建议 cron 每晚跑一次
```

镜像解决在线 API 做不到的一件事：**某编码的全部先例**。API 的 term 是全文检索，
客户端只能在拉回的前几页里过滤；本地全量 tariffs 索引给出精确计数（实测
8507.60.00 共 80 条、8506.50.00 共 6 条）。建好后归类对比弹窗自动展示完整反查
（离线毫秒级），未建镜像则静默跳过、在线检索仍可用。

- 枚举用 `term=*` + 年度日期切片绕过服务端 10k 深翻页窗口，超限年份自动对半
  细分（1999/2000/2002/2003 实测超限）；起始 1966 年（官方说"1989 至今"，
  实测更早的也在）。约 280 条无日期裁定无法枚举，同步报告里的"差额"即它们。
- **撤销状态每晚全刷**：撤销动的是老记录（N232914 是 2012 年的，2015 年才被
  撤销），增量拉新裁定看不见这种变化。全量元数据仅 ~600 请求。
- 同步报告沿用 build_db 原则：状态变化（现行 → 已撤销）逐条打印，绝不静默。
- **现行税则对账**：裁定引用的编码若已不在今天的 HTS（90 年代裁定 42% 中招、
  2000 年代 28%——税则修订不触发撤销标记），界面上删除线 + ⚠ 标出。
  归类思路可参考，编码必须以现行税则重新落位。

### 语义索引（`scripts/cross_embed.py`，二期）

```bash
python scripts/cross_embed.py            # 全量嵌入 → cross.db 的 vec0 表（~4h，本机 ollama）
python scripts/cross_embed.py --rebuild  # 换模型/换嵌入口径后清空重建
```

把 22 万条裁定的 **subject 嵌入向量**，中文描述直接语义找先例，不再依赖先翻英文关键词。

- 模型 `qwen3-embedding:8b` + 查询侧 instruct 前缀（实测 P@5 0.86，胜 0.6b 的 0.77）；
  MRL 截断存 1024 维（0.9GB，vs 满维 3.6GB），221k 规模 top-50 检索 36ms。
- **嵌入文本 = subject + 编码的官方 HTS 品名**。CROSS 约 2% 的 subject 词不达意
  （"Request for Further Review of Protest..."），但每条都带精确编码、官方品名本地就有，
  拼进去零成本救回：实测一条零产品信号的餐具裁定，查"餐具勺子叉子"从裸嵌入的
  末位（13/13）跳到首位。这比"读全文让大模型生成描述"便宜几百倍，效果相当。
- 每晚 `cross_sync` 后接着跑，只嵌新增（几十条秒级）；`launchd` 已配 `sync && embed`。
- 未建索引 / ollama 离线 → 语义检索降级为提示，编码反查与在线检索不受影响。

### 正文深读（`cross.fetch_ruling_text` + `ai.deepread_precedents`，三期）

语义召回找到"名字像"的候选后，拉这几条的**正文**交给本地大模型精读，指出哪条货物
真的像、并**逐字摘录**决定归类的 CBP 原文。

- 只对召回的前几条**按需拉正文**（永久缓存，正文不可变），不做 22 万全量下载。
  PDF→pdfplumber / OLE2→可打印串提取，探针实测 ~100%。
- AI 角色严格限制在"读 + 定位 + 摘录"，**不改写、不生成归类意见**——先例的价值
  在于能拿 CBP 原话跟海关讲，一经转述就作废。
- **原文校验是安全底线**：AI 引用的句子须逐字出现在正文里（归一大小写/空白/弯引号/
  破折号后子串比对），否则界面标红「引用存疑」。真实验证：读 N286124 判 high、
  摘录 `non-rechargeable ... 8506.50.0000` 逐字命中、校验通过。
- 需配置 AI provider（见「AI 功能配置」）。未配置则深读入口不显示，前两层照常。

例：8506（原电池）vs 8507（蓄电池）的分界是"能不能充电"，本地数据抽不出这个条件，
但 CBP 在 **N286124** 里已明文判过——不可充电锂电池 → 8506.50.0000，
可充电锂离子 → 8507.60.0020。深读（③）读到的正是这条正文里那句原文。

```bash
python scripts/cross.py "lithium primary battery non-rechargeable" --codes 8506.50.00,8507.60.00
```

### 各层要点

- **`term` 是全文检索，不是编码字段查询。** 搜 `8507.60.00` 命中的是正文提到该编码的
  裁定（多为 protest / drawback），不是"归到这个码"的裁定。要找某编码的先例，
  用商品英文名检索再按 `tariffs` 过滤——`precedents()` 就是干这个的。
- **「候选外编码」是归类信号**：CBP 把同类货判到了你没考虑的编码上。标「同品目」的
  与候选同 4 位品目、仅子目不同，最值得先看。
- **引用前必看状态**。裁定会被撤销/修改（实测 `battery` 前 300 条里有 12 条失效）。
  引用一条已撤销的裁定比不引用更糟。模块把三个来源字段归一为「状态」，
  失效的不隐藏但排在后面并标注被谁撤销。
- **多条结论冲突时：HQ > NY，新 > 旧，事实最接近 > 词句最像**。HQ（总部）可撤销
  NY 的裁定，反之不行（实测 2399 条样本里 25 次撤销全部由 HQ 发起）。
  `precedents()` 按现行在前、HQ 在前排序，组内保持 CROSS 相关度。
- **老裁定的 6 位码可能已被 HS 修订改掉**，而 CROSS 不会为此标记撤销——revoked
  只防"结论被推翻"，防不住"编码被搬家"。早于 HS 2012/2017/2022 修订的裁定
  带「版本提示」，引用前须回现行税则核对编码仍然存在。
- **裁定不是保护伞**：只对申请人的该笔交易具法律约束力，他人可参考但货物有差异时
  未必适用——引用前须读全文事实描述段，相似不等于相同，差一个参数结论可能相反。
- 接口公开无需认证，数据属公共领域（data.gov 标注 `usa.gov/government-works`），
  但**无公开文档、无 SLA**。因此本模块超时 10 秒、结果落 `.cache/cross/`（失败也短暂缓存，
  避免离线时每次干等），且**任何失败都返回 `{"error": ...}` 而非抛异常**——
  CROSS 是锦上添花，本地税则查询才是主链路。
- 裁定正文**不做 AI 转述**，只给原文链接。先例的价值就在于它是 CBP 的原话，
  一经转述就不能拿去跟海关讲了。

---

## 税率引擎说明（v1.2）

**税率形态识别**（`scripts/rate.py` 的 `parse_rate`）：

| 形态 | 示例 | 处理 |
|---|---|---|
| 免税 | `Free` | 等效从价 0% |
| 从价 | `6.5%` | 直接比较 |
| 从量 | `1¢/kg`、`$1.646/kg`、`0.9¢ each` | 需单位货值折算 |
| 复合 | `46.3¢/kg + 14.9%` | 从价部分直接比较，从量部分需折算 |
| 引用 | `The duty provided in the applicable subheading` | 无法折算，标"需人工" |
| 复杂 | `$1.61 each + 4.4% on the case...` | 分部件税率，标"需人工" |

**总税负 = 基础等效从价 + 301 加征 + 附加税**。从量/复合/复杂税在未提供单位货值时标记"需折算"，避免误导。

**搜索匹配**：英文品名精确词 + 前 5 字符词干索引（`battery` 可命中 `batteries`），
多关键词 AND 匹配；同时支持编码前缀匹配（`8507` → 8507 章）。

---

## 界面主题

页面右上角可切换两套主题，选择记在浏览器 `localStorage`（键 `hts_theme`），下次打开保持：

| 主题 | 观感 |
|---|---|
| **现代**（默认） | 浅色、干净，适合演示与打印 |
| **黑神话** | 暗黑国风：墨黑 `#0a0806` + 鎏金 `#d8a63f` + 朱砂 `#e2503f` + 青瓷 `#79b493`；宋体标题描金、朱砂印章徽标、回纹分隔、卡片对角金角、青铜表头、水墨晕染底 |

也可用 URL 参数直接指定，便于分享指定主题的链接：
`http://127.0.0.1:5000/?theme=wukong`（`?theme=modern` 同理，优先级高于本地记忆）

两点刻意的克制：宋体只用于标题/表头/按钮等大字（宋体小字在屏幕上发虚，整页宋体会拖慢读表速度）；
全部装饰均为静态，无循环动画。

**新增主题**只需在 `templates/index.html` 的 `<style>` 顶部加一个
`[data-theme="你的主题名"] { ... }` 变量块（50 个 token 全覆盖即可），
下面所有样式规则无需改动 —— 通用规则里不存在硬编码色值。
装饰层同样由 token 驱动（`--ornament` / `--rule-image` / `--ornament-color`），
在不需要的主题里置空即可自动失效，无需写选择器去关闭。

---

## 加征开关配置（v1.5）

**两种配置方式：**

**方式 A：Web 端开关（推荐）**——打开 http://127.0.0.1:5000 →「编码查询」右上角 **⚙ 加征开关**：
勾选/取消「中国 301 加征」「FLIP 301 强迫劳动关税」→ 保存（立即生效，无需重启），查询 / 估算 / 税率搜索三端同时裁剪。

**方式 B：直接编辑** `measures_config.json`（项目根目录）：

```json
{
  "measures": {
    "cn301": true,
    "flip301": true
  }
}
```

- `cn301`：中国 Section 301 加征（关闭后查询/估算/搜索不含"301加征"字段、总税负不叠加，301 判定/9903 子目信息仍显示）
- `flip301`：FLIP 301 强迫劳动关税（关闭后不含"FLIP 301加征"字段、总税负不叠加）
- 配置缺失或某项缺失时**默认全部启用**，保证既有查询结果不变；改动配置后查询即时生效（无需重启）

---

## AI 功能配置（可选，默认不启用）

AI 层**可插拔**：不配置则 AI 功能返回提示，其余功能（查询/搜索/估算/变动）完全离线可用。
核心查询始终零网络；只有 AI 功能按你的配置访问模型服务。

**两种配置方式（任选其一）：**

**方式 A：Web 端手动配置（推荐）**——打开 http://127.0.0.1:5000 →「AI 助手」→ 右上角 **⚙ AI 服务配置**：
选择服务类型、填 base_url / model / api_key → 保存（立即生效，无需重启）→ 点「测试连接」验证。
api_key 仅保存在本地 `ai_config.json`，界面只显示掩码、绝不回显明文。

**方式 B：直接编辑** `ai_config.json`（项目根目录）：

```json
{
  "provider": "openai_compat",
  "base_url": "https://api.openai.com/v1",
  "api_key": "你的密钥",
  "model": "gpt-4o-mini",
  "temperature": 0.2,
  "timeout": 60
}
```

- `"provider": null` —— 不启用（默认）
- `"provider": "ollama"` —— 本地模型（`base_url` 默认 `http://127.0.0.1:11434`，`model` 如 `qwen2.5:7b`），完全离线。
  可选 `"think": false`（默认）：qwen3 等模型的**思考模式**默认关——本项目每次调用都是按格式出 JSON，
  隐藏推理链只烧时间（实测同一提示开 3.8s / 关 0.2s，搜索页 AI 辅助 27s → 秒级）；要开设 `true`
- `"provider": "openai_compat"` —— 任何 OpenAI 兼容 API：OpenAI 官方（如上例）、DeepSeek（`https://api.deepseek.com/v1` + `deepseek-chat`）、通义千问（`https://dashscope.aliyuncs.com/compatible-mode/v1` + `qwen-plus`）、自建中转站（改 `base_url`）等

**AI 归类流程**：LLM 提取关键词 → 本地税则库召回候选 → LLM 精排 → 本地引擎校验 301 与税负。
AI 结果仅供参考，正式报关归类以 CBP 裁定为准，请人工复核。

---

## 输出结果说明（编码查询）

每行输出 15+ 列（加征字段"301加征 / FLIP 301加征"随 `measures_config.json` 配置启用）：

| 列 | 说明 |
|---|---|
| 输入编码 | 你输入的编码（规范化格式） |
| 8位子目 | 用于 301 判定的 8 位子目 |
| 商品描述 | 官方品目描述 |
| 一般税率 / 特殊税率 / 第二栏税率 | 基础税率（非 301 部分） |
| 原产地 / 原产地代码 | 中国 / 越南 / 其他国家（国家代码） |
| 301判定 | 是 / 是(已排除) / 否 / 是(豁免/0%) / 无法判定 / 不适用（非中国原产） |
| 301排除 / 301排除明细 | `已排除 至 2026-11-09`（整号排除，加征已归零）/ `待核：3 整号 / 2 描述`（需人工核对）；明细含每条的覆盖方式、统计号、原文、有效期与 Chapter 99 PDF 页码 |
| 9903子目 | 命中的 Chapter 99 子目，如 9903.88.03 |
| 301加征 | 加征比例，如 +25%、+100%（配置启用时输出） |
| FLIP 301加征 / FLIP 301说明 | 强迫劳动关税 +12.5%/+10%、`豁免`（ANNEX II 且无范围限制）、`+12.5%(范围存疑)`（ANNEX II 但带 Scope Limitations，已按不豁免计，需人工核实用途）（配置启用时输出） |
| 来源 | 每条税负判定的官方出处列表：文件 + CSV 行号 / PDF 页码 + 范围限制；Web 端点击 📄 查看详情并可打开原文定位 |
| 301 flip历史 / 301 flip变化 | 此前的 301 档位与变化（2026 现行） |
| 越南措施 | 越南/其他国家的适用措施说明 |
| 附加税 | 反倾销/反补贴等附加税（若有） |
| 备注 | 提示（旧编码、6位无法判定、豁免核对、加征禁用标注等） |

## 301 加征档位速查（2026 现行版）

| 9903 子目 | 加征 | 对应清单 |
|---|---|---|
| 9903.88.01 | +25% | 301 List 1（2018.07 起） |
| 9903.88.02 | +25% | 301 List 2（2018.08 起） |
| 9903.88.03 | +25% | 301 List 3（2018.09 起） |
| 9903.88.04 | +25% | 301 List 3 部分（2019 起） |
| 9903.88.15 | +7.5% | 301 List 4A（2019.09 起，2020.02 降至 7.5%） |
| 9903.91.01 | +25% | 2024 审查新增（关键矿产等） |
| 9903.91.02 | +50% | 2024 审查新增（光伏组件等） |
| 9903.91.03 | +100% | 2024 审查新增（纯电动车等） |
| 9903.91.05 | +50% | 2024 审查新增（半导体等） |
| 9903.91.06 | +25% | 2026 生效（锂电池等） |
| 9903.91.07 | +50% | 2026 生效 |
| 9903.91.08 | +100% | 2026 生效 |
| 9903.92.10 | +25% | 2024 审查新增（船岸起重机） |

（完整档位见 `data/sec301_db.json` 中 `c99_percent` 字段，含 9903.85.67/68 的 +200% 等特殊条目）

## 数据更新方法

官方数据每月都可能调整。三份源文件的官方地址（2026-09 实测）：

| 本地文件 | 官方地址 | 版本探针 |
|---|---|---|
| `htsdata.csv` | [USITC 全量导出](https://hts.usitc.gov/reststop/exportList?from=0100&to=9999&format=CSV&styles=false) | [`/reststop/currentRelease`](https://hts.usitc.gov/reststop/currentRelease) → `{"name":"2026HTSRev17"}` |
| `China Tariffs_*.pdf` | [USITC 托管的 China Tariffs](https://hts.usitc.gov/reststop/file?release=currentRelease&filename=China+Tariffs)（**不在 USTR 站上**） | 响应头 `Content-Disposition` 文件名带 Rev 号；首页有 "Last Updated" |
| `FLIP 301 ... FINAL.pdf` | [USTR 最终行动 FRN](https://ustr.gov/sites/default/files/files/Press/Releases/2026/FLIP%20301%20Investigation%20Final%20Action%20FRN%207-23-26%20FINAL.pdf) | `ETag` / `Last-Modified` |
| `Chapter 99_*.pdf` | [USITC HTS 第 99 章](https://hts.usitc.gov/reststop/file?release=currentRelease&filename=Chapter+99)（301 排除清单 U.S. note 20 正文；China Tariffs 那份 PDF 不含排除） | `currentRelease` 版本号 |

### 自动检测：`scripts/check_sources.py`

```bash
python scripts/check_sources.py                   # 探测三份文件是否有新版本，打印报告；有更新退出码 3
python scripts/check_sources.py --apply --rebuild # 下载覆盖（旧文件备份到 output/sources_backup/<时间>/）并重建数据库
python scripts/check_sources.py --json            # 机器可读
```

- **判"有没有更新"只看内容哈希**：USITC 每次发版都把 China Tariffs 改名（Rev15→Rev17）但内容常一字不变，
  按名判会误报；CSV 导出的换行符会漂，哈希前抹平。版本号 / ETag 只用来省流量（没变就不重下 4MB）。
- 比对对象是**本地文件本身**，手动替换过文件也不会错判。
- htsdata 有更新时报告**行级差异样例**（新增/删除了哪些编码），`--apply` 前先看一眼改的是什么。
- 本地 `China Tariffs_2026HTSRev15.pdf` **文件名不随远端改**（`build_db.py` / `app.py` 按名引用），
  远端版本号记在 `data/.sources_state.json`，Web「📄 查看数据来源」弹窗展示每份文件"与官方一致 / 官方已更新"。
- **局限**：FLIP FRN 的 URL 指向 7-23-26 这一份通知，USTR 若发布**新的**修改通知是新 URL，脚本探测不到，
  需人工关注 [USTR 新闻页](https://ustr.gov/about/policy-offices/press-office/press-releases)（报告里有提醒）。

**定时**：已配 `launchd`（`~/Library/LaunchAgents/com.hts-agent.check-sources.plist`）每天 08:30 跑
`check_sources.py --notify`——有更新弹 macOS 通知，报告追加到 `output/check_sources.log`；
**只检查不自动覆盖**，覆盖+重建要人看着做（`--apply --rebuild`），重建后到「数据变动」核对变动清单。

手动更新（不用脚本）：下载上表三个地址替换对应文件 → `python scripts/build_db.py`。
重建时自动与上一版本对比，生成**数据变动清单**（Web 端「数据变动」可查看），
方便你第一时间发现"客户常查的商品税率变了"。

FLIP 301 豁免的范围限制、页码与 `Ex` 档描述原文（`data/flip301_exemptions.json` 的
`universal_scopes` / `universal_pages` / `universal_ex_desc` 等键）由独立脚本提取：
`python scripts/extract_flip_scopes.py`（FLIP FRN 更新后重跑一次即可），
**改完要跑 `scripts/build_db.py`**——这份 JSON 是编译进 `sec301_db.json` 的，不重建不生效。

301 排除清单（`data/sec301_exclusions.json`）由 `python scripts/extract_exclusions.py`
从 `Chapter 99_*.pdf` 提取（条目正文）+ `htsdata.csv`（有效期）。
**Chapter 99 或 htsdata.csv 任一更新都要重跑它**，`check_sources.py --apply --rebuild` 已自动串好。

## 判定逻辑与重要局限

**判定逻辑**：输入编码取前 8 位 → 在 USTR 清单中查找 → 命中则返回对应 9903 子目及加征比例。

**必须注意的局限（重要）**：

1. **中国 301 的排除（Exclusion）**：已判定，但只有**整号排除**能自动判免（条目正文就是一个统计号，纯粹是编码 + 日期问题）。**按描述授予的排除本工具不自动判免**——同一税号下有的款符合描述、有的不符合，编码本身回答不了，自动判免会直接造出错误申报；工具只给出条目原文与页码，需人工逐条核对。另：排除授予到 **10 位统计号**，只给 8 位时不判免。
   ⚠ 排除有硬到期日：9903.88.69 / .70 均至 **2026-11-09**。到期后若不重抓 Chapter 99 并重跑 `extract_exclusions.py`，工具会继续按已失效的排除判 0%——**少报**的方向，务必让 `check_sources.py` 的每日探测保持运行。
   ⚠ 这与 **FLIP 301 的 ANNEX II 豁免**是两套东西：后者按税号列示、已逐编码判定，带范围限制的会标"范围存疑"（见上表）。
2. **原产地规则**：301 关税仅适用于**中国原产**商品。经第三国实质性转型的产品不适用。
3. **编码版本**：本工具基于 2026 现行 HTS（2022 年后的新编码体系）。旧版编码（如 8703.23.00）会提示"未找到"，请使用现行编码（如 8703.23.01）。
4. **总税率估算**：从价税直接相加；从量/复合税需按单位货值折算（Web 逐行填、命令行传 `--unit-value`），分部件复杂税无法折算，标"需人工"。估算仅供参考。
   ⚠ **数量与单位货值必须按同一个计量单位**：从量税折算走的是 `每单位税额 ÷ 单位货值`，所以单位货值是"每 ‹税则计量单位› 多少美元"。按"件"填一个按 kg 计税的商品，货值与税费会整行算错——Web 估算表已把官方计量单位列出来，按那一列填。
5. **官方口径**：本工具仅供内部效率参考，正式报关以 CBP 裁定和 USTR 官方公告为准。

## MongoDB 产品库对账 / 回写（可选）

若你的产品数据在 MongoDB（`products` 集合，字段 `HS_CODE` 与 `加征.加征_301`），
可用本项目的官方判定校准库里的 301 加征。**Web 与命令行查询功能不依赖数据库**，
不用这两个脚本可以完全忽略本节。

**凭据一律从 `.env` / 环境变量读取，不写在代码里**：

```bash
cp .env.example .env       # 然后填入 MONGO_LOCAL_USER / MONGO_LOCAL_PASS 等
```

```bash
# 只读对账：列出库值与官方判定不一致的产品，出 xlsx + csv 报表
.venv/bin/python compare_db_301.py
.venv/bin/python compare_db_301.py --database remote --limit 500

# 回写校准：默认计划模式只打印将改什么，不写库
.venv/bin/python update_db_301.py
.venv/bin/python update_db_301.py --execute    # 执行前自动备份（库内副本 + 本地 JSON）
```

回写只改 `加征.加征_301` 与 `豁免代码` 两个字段；对「官方判定无、但库里有记录」的条目
只提示不删除。改完建议重跑 `compare_db_301.py` 验证。

---

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
# 或不装 pytest：.venv/bin/python -m unittest discover -s tests -v
```

覆盖：税率解析（Free/从价/从量/复合/引用/复杂）、等效折算、总税负、关键词搜索排序、
AI 层流程（假 Provider）、Web API 集成、导出防公式注入、批量统计口径、配置缓存失效、
多措施（301 flip 历史 / 越南原产地轨道 / FLIP 301 国家查表与数据缺失兜底 /
措施开关配置两档 / 中国回归）。

## 环境依赖

- Python 3.10+（开发与 CI 使用 3.12）
- 完整依赖见 `requirements.txt`；核心为 `fastapi` / `uvicorn` / `python-multipart` /
  `pandas` + `openpyxl`（Excel 读写）/ `pdfplumber`（构建阶段解析 PDF）/ `httpx`（AI 层）
- MongoDB 对账脚本另需 `pymongo` + `python-dotenv`（已含在 `requirements.txt`，不用可忽略）

## 安全须知

- `ai_config.json`（含 API Key）与 `.env`（含数据库口令）**已在 `.gitignore` 中，切勿提交**；
  仓库内只保留 `ai_config.example.json` / `.env.example` 模板。
- Web 服务仅监听 `127.0.0.1`，配置类接口（AI 配置 / 加征开关）无鉴权，
  **请勿改为 `0.0.0.0` 暴露到公网**；确需内网共享请自行加反向代理与认证。
- 导出的 CSV/XLSX 已对 `=` `+` `-` `@` 开头的单元格做公式注入中和。
