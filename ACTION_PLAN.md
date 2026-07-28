# Research Radar 科研日报系统 · 具体行动方案

> 版本：V3（按实际部署状态编写）· 更新日期：2026-07-28
> 部署环境：WSL2 Ubuntu · 项目根目录 `~/research_radar` · Python 一律使用 `venv/bin/python`

---

## 1. 系统目标

每天自动抓取 PubMed / bioRxiv / arXiv / 顶刊目录的最新文献，按用户的科研画像
（物种、方法、工具、概念四组关键词 + AI 语义评分 + 期刊声望 + 外部趋势信号）打分排序，
以邮件形式发送**日报（15 篇）**、**周报（15 篇）**、**月报（15 篇）**，
并通过邮件内一键反馈按钮形成关键词权重学习闭环。

---

## 2. 总体架构

```
爬虫层 crawler/          过滤层 processing/         排序层 ranking/
  pubmed.py        ──→    keyword_filter.py  ──→     scoring.py
  biorxiv.py              paper_analyzer.py          trends.py
  arxiv.py                （AI 相关性评分）
  top_journals.py
        │                                               │
        ▼                                               ▼
              database/db.py（SQLite：papers / recommendations）
                                │
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
  email/generate_email.py  email/generate_digest.py   feedback/
  （日报 15 篇）            （周报/月报 15 篇）          server.py  一键反馈服务(8710)
                                                     learning.py 反馈学习
        │
        ▼
  keyword_engine/（关键词扩展与审核：expand → review → merge）
```

调度入口：`daily_run.sh`（crontab 每天 08:30），节假日决策由 `scheduler/holiday.py` 输出。

---

## 3. 目录结构与关键文件

| 路径 | 职责 |
|---|---|
| `crawler/pubmed.py` `biorxiv.py` `arxiv.py` | 按 `--since` 日期抓取三源文献入 `papers` 表 |
| `crawler/top_journals.py` | 按 ISSN 直抓顶刊清单（`config/top_journals.yaml`，16 本：Nature/Science/Cell/NEE/Nature Methods 等），**不经关键词检索**直接进主池 |
| `processing/keyword_filter.py` | 关键词四组（species 5 / methods 8 / tools 9 / concept 10）精确匹配，输出 `rule_score` 与 `passed_filter`；negative 排除词直接剔除 |
| `processing/paper_analyzer.py` | AI 语义评分 0-10（两批候选：关键词通过者 Top 20 + 顶刊未评分者 Top 30），prompt 在 `prompts/relevance_scoring_prompt.txt` |
| `ranking/scoring.py` | 四维加权总分 → 定级 → 四层梯队选满 15 篇写入 `recommendations`；`--pub-date` 可按发表日期过滤；`--offline` 趋势维度只用本地语料 |
| `ranking/trend_signals.py` | 外部趋势信号：PubMed 近 30 天发文热度（esearch count，按日缓存）+ 顶刊当期命中；本地语料频次作离线/失败回退 |
| `ranking/trends.py` | 聚合近 30 天推荐，AI 生成约 200 字趋势总结（日报末尾展示） |
| `email/generate_email.py` | 日报 HTML：Part1 一句话新闻摘要 / Part2 详细信息卡片（中英摘要+推荐理由+一键反馈）/ Part3 今日趋势总结；`--send` 发信 |
| `email/generate_digest.py` | 周报（`--days 7`）/月报（`--days 30`）：Part1 趋势总结 / Part2 分布统计 / Part3 重点论文清单（含评分、中文摘要、推荐理由、链接、反馈按钮） |
| `feedback/server.py` | 127.0.0.1:8710 一键反馈：`/feedback?paper_id=..&rating=good|ok|bad|read|star`，写 `input/user_feedback/YYYY-MM-DD.jsonl`，无弹窗 |
| `feedback/learning.py` | 负反馈降权（locked 种子词不降）；正反馈经 AI 提炼候选新词进 `output_candidates.yaml`（pending，**必须人工审核**） |
| `keyword_engine/` | `expand_keywords.py`（AI 扩展）→ `review_candidates.py`（人工审核）→ `merge_to_config.py`（合并入 `config/keyword_config.yaml`） |
| `scheduler/holiday.py` | 中国法定节假日决策：节假日跳过日报、节后合并发送；周报每周五（逢节假日提前）；月报每月底（逢节假日提前） |
| `daily_run.sh` | 每日流水线入口，单步失败不中断，日志 `logs/daily_YYYY-MM-DD.log` |
| `config/model.yaml` | AI 模型配置：gpt-5.4 @ `https://dcsapi.dcs.cloud/api/aigress/unified/v1` |
| `input/seed_keywords.txt` | 核心科研画像（locked，权重最高，负反馈不降权） |
| `input/keywords.txt` | 用户确认的完整关键词表（生成 `keyword_config.yaml` 的来源） |

---

## 4. 评分体系（`config/scoring.yaml`）

四维归一化到 0-10 后加权，总分 0-10：

| 维度 | 权重 | 计算方式 |
|---|---|---|
| research_relevance | 0.3 | 关键词命中 `rule_score` ÷ 当日候选池最大值 × 10 |
| ai_semantic_relevance | 0.3 | AI 语义分（缺失按 5；<3 为 AI 否决，仅作兜底候选） |
| journal_influence | 0.3 | 内置期刊分档（顶刊 10 分） |
| trend_value | 0.1 | 外部趋势信号：PubMed 近 30 天发文热度（0-8）+ 顶刊当期命中（+2），见 `ranking/trend_signals.py` |

**trend_value 外部信号**（2026-07-28 起，替代原 concept 词表镜像）：取论文显著词
（命中的配置关键词 ∪ 标题特征词），逐词查 PubMed 近 30 天 title/abstract 发文量
（esearch count，≤3 请求/秒，按日缓存 `logs/pubmed_heat_cache.json`；网络不可用时
回退本地 papers 表文档频次），`heat = Σ log1p(count)` 按候选池最大值归一化到 0-8；
期刊在顶刊清单内再 +2，封顶 10。

**定级阈值**：≥7 Must Read · 5-7 Important · <5 Relate。

**四层梯队选满 15 篇**：A 关键词通道 → B 顶刊+已评 AI → C AI 否决但关键词或顶刊命中 → D 放宽 30 天去重兜底；negative 排除词全层剔除；30 天内不重复推送。

---

## 5. 每日流水线（`daily_run.sh`，cron 08:30）

```
holiday.py 决策（SKIP / DAILY_SINCE / WEEKLY / MONTHLY）
  → crawler: pubmed / biorxiv / arxiv / top_journals（--since）
  → filter: keyword_filter
  → analyze: paper_analyzer（AI 评分）
  → ranking: scoring（定级 + Top15）
  → email: generate_email --send（日报）
  → 若周五（逢节假日提前）：generate_digest --days 7 --send（周报）
  → 若月底（逢节假日提前）：generate_digest --days 30 --send（月报）
  → feedback: learning（反馈学习）
```

节假日规则：法定节假日整天跳过；节后首个工作日发送**合并日报**（爬虫 `--since` 取节前最后非节假日，覆盖节假日空窗）。

---

## 6. 邮件规格

**日报**：15 篇按评分降序。Part 1 今日论文新闻摘要（一句话：解决什么问题→什么方法→什么创新→什么结果）；Part 2 详细信息卡片（中英文摘要、推荐理由、期刊·日期、DOI/PubMed 链接、一键反馈按钮）；Part 3 今日趋势总结（AI 生成）。

**周报/月报**：汇总窗口内全部推荐去重后按评分取 Top 15。Part 1 趋势总结（总览/共同技术趋势/跟踪线索分块）；Part 2 分布统计（定级分布、期刊分层、来源期刊 Top5、高频关键词 Top10）；Part 3 重点论文清单（含总分、中文摘要、推荐理由、链接、反馈按钮）。

**发信**：163 SMTP（`xiao020327@163.com`）→ `zhangyanshu@genomics.cn`（凭据在 `.env`，不入库不入文档）。

---

## 7. 反馈学习闭环

1. 用户在邮件点 **相关 / 不相关 / 已读 / 收藏** → 8710 端口服务记录到 jsonl（同日同篇同评级去重，页面直接提示"已记录"）。
2. 每日流水线末尾 `feedback/learning.py` 处理：
   - `bad`：该篇命中的词条 weight −1（下限 1）；**种子词 locked=true 永不降权**；
   - `good` / `star`：AI 从论文提炼新候选词 → `output_candidates.yaml`（status: pending）；
   - `read` / `ok`：只记录，不调整。
3. 候选词必须经 `review_candidates.py` **人工审核**后 `merge_to_config.py` 入库，绝不自动合并。

---

## 8. 运维速查

```bash
cd ~/research_radar && source venv/bin/activate

# 手动跑某日全流程（不发邮件可去掉 --send）
python ranking/scoring.py --pub-date 2026-07-22     # 按发表日期重算
python email/generate_email.py --date 2026-07-22 --send
python email/generate_digest.py --days 7 --send     # 手动周报
python -m pytest tests/ -q                          # 66 个测试
tail -f logs/daily_$(date +%F).log                  # 当日日志
```

- 换 AI 模型：改 `config/model.yaml` + `.env` 中的 key（当前 gpt-5.4，网关兼容 chat/completions）。
- 增删顶刊：改 `config/top_journals.yaml`（name 必须与 PubMed NLM 全名一致）。
- 改关键词：改 `input/keywords.txt` → 重新生成 `config/keyword_config.yaml`；**关键词内容与权重属用户决策项，必须先确认**。

---

## 9. 待办与已知问题

| # | 事项 | 状态 |
|---|---|---|
| 1 | species 组缺 Drosophila/昆虫/节肢动物等泛化物种词（此前整表替换 keywords.txt 时丢失），导致 Nature 果蝇出生序论文关键词通道得 0（总分 4.40 仅 Relate，补齐后约 8.4 应为 Must Read） | **等用户确认词表后修改** |
| 2 | ~~trend_value 实为 concept 词表命中的镜像，与关键词通道重复计分~~ **已解决（2026-07-28）**：trend_value 改为外部信号（PubMed 近 30 天发文热度 + 顶刊当期命中，见第 4 节），同时关键词与 AI 语义权重拉平为 0.3/0.3/0.3/0.1 | 已完成 |
| 3 | methods/concept 组为长短语精确匹配，措辞对不上易漏命中，可考虑拆宽 | 待用户决策 |
| 4 | 163 SMTP 曾触发 550 风控（短时高频发信），已换发信箱；如再发需控制频率 | 已缓解 |
