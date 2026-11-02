# 公众号文章自动化 Agent

一个面向**单人内容运营**的公众号生产流水线：抓热点 → 聚类评分 → LLM 按模板生成 → Playwright 推送草稿 → **人工审发** → 效果数据回流。

核心原则只有一句：**机器负责采、编、排，人负责审、发。**
系统可以自动化到"草稿箱为止"，但绝不自动发表、绝不自动群发。

---

## 它解决什么问题

一个人做内容，每天的真实成本不是"写"，而是三件琐事：

1. 在十几个信息源里翻今天有什么值得写的；
2. 把素材整理成一篇有观点的文章（而不是把新闻复述一遍）；
3. 排版、做封面、贴进公众号后台。

这个项目把这三件事自动化，只把**判断**留给人。它还多做了一步别人常忽略的事：
文章发出去之后，把阅读/点赞/**分享**数据抓回来，反过来教选题和标题——
因为实测下来，公众号涨粉几乎只靠分享（约 1 次分享 ≈ 15~20 次阅读），而"热度高"并不等于"会被转发"。

---

## 流水线

```
① 抓取        8 个 RSS 源 → 关键词过滤 → URL 去重入库
② 分析        清洗 → 跨源归一化评分 → 时效衰减 → 聚类 → 主题报告
③ 生成        两步：先出候选论点 → 人选一个 → 再写成文章
④ 推草稿      Playwright 建稿 → 排版 inject → 自动封面 → 保存为草稿
⑤ 人工审发    在线编辑 / 通过 / 驳回 / 打开公众号后台自己发
⑥ 数据回流    反查发表记录 + 抓阅读/点赞/分享/推荐/留言
```

环节之间有状态机衔接，每一步都写断点（`run_logs` 只追加），失败可以定位到"第几稿、为什么没过"。

### 状态机

```
generated ──approve──> approved ──push──> pushed ──发表──> published ──回流──> backfilled
    │                                                          
    ├── edit ──> edited
    ├── reject ─> rejected
    └── 熔断 ──> generation_failed
```

### 界面（7 个页签 + 1 个独立文章窗口）

| 页面 | 做什么 |
|---|---|
| 运营看板 | 今日抓取/分析/生成、待办、滞留告警 |
| 分析报告 | 按时间范围实时聚类，勾选素材 → 生成候选论点 |
| 文章库 | 全量稿件、状态筛选、批量废弃/清理 |
| 数据回流 | 已发布文章的反馈指标与趋势、后台核对状态 |
| 人工审核 | 阅读正文、在线编辑、通过/驳回、推送草稿 |
| 运行监控 | 当前/最近一次流水线的阶段、耗时、日志、失败原因 |
| 控制面板 | 运行参数 / 写作立场 / 推送 / 排版 / 封面 / 模板库 |

---

## 技术栈

- **后端**：Python 3.11+ · FastAPI · SQLite（WAL）· APScheduler · Playwright · OpenAI 兼容接口
- **前端**：Vue 3 · Vite（hash 路由，构建产物由后端同源托管）
- **存储**：单文件 SQLite（`data/app.db`），无外部服务依赖

---

## 快速开始

### 1. 安装

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -e ".[dev]"
cp .env.example .env          # 填入 LLM_API_KEY
```

`.env` 关键项：

```ini
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_API_KEY=sk-your-key-here
LLM_MODEL=qwen3.6-plus
LLM_THINKING=auto             # auto / on / off，见下方"生成慢的根因"
```

### 2. 启动

```bash
# 后端（首次启动自动建库建表）
uvicorn app.main:app --port 8000
```

前端有两种跑法：

```bash
# A. 日常使用：先构建，后端会把 dist 挂到 /，一个地址搞定
cd wechat-agent-frontend && npm install && npm run build
# 打开 http://127.0.0.1:8000

# B. 开发模式：Vite 热更新，/api 由 Vite 代理到 8000
cd wechat-agent-frontend && npm run dev   # http://127.0.0.1:5173
```

Windows 上可以直接双击 `start_app.bat`：自动建 venv、装依赖、下载 Playwright 浏览器、
检查端口归属（区分"本服务的旧进程"和"别人占用"）、等就绪后打开浏览器。

### 3. 不接外部依赖先跑一遍

```bash
python -m app.pipeline_demo     # 假数据源 + mock LLM，全链路走通
python -m app.seed_demo         # 灌一批演示数据，便于看界面
```

### 4. 测试

```bash
pytest
```

---

## 值得一说的几个设计决定

这些不是"最佳实践"，多数是被现实打过之后改出来的。

### 生成慢的根因不是提示词，是模型的隐藏思考链

实测一次 165 秒的生成里，输出 9,589 tokens 中约 8,000 个是**不返回给用户的隐藏推理**，
首字要等 148 秒。关掉思考模式后：**165 秒 → 15~19 秒**，正文长度与结构不变。

配置项 `LLM_THINKING`（`auto` / `on` / `off`）：`auto` 只对 DashScope/Qwen 下发
`enable_thinking=False`——这是供应商特有参数，对 OpenAI/Ollama 下发可能直接 400。

**顺手得到的结论**：本地环节（组装提示词、校验、排版）总共不到 20ms，
精简提示词几乎没有收益。优化要打在最贵的那一段上。

### 重试必须"靶向"

盲重试的代价是一整次 LLM 调用。所以校验没过时，会把**上一稿没过审的具体原因**回灌进下一次提示词，
让模型针对性修改，而不是靠温度随机性撞过去。

### 一次性误杀 = 白烧一整次生成

违禁词检查必须用**词组**（"最好/最佳/最优"），不能用单字"最"——
后者会把"最关键""最重要的变量"这类正常分析表达全部判死。
一个假阳性在用户眼里就是"生成卡住了"。

### 「已发布」只能有一个真源

曾经排期、看板刷新、看板读取、反查各写一套 SQL，结果是"库里有 7 行数据，看板只显示 5 篇"。
现在统一到 `_published_articles()`：**判据只有状态 + 发布时间**，
草稿链接只是匹配手段、不是身份判据。

### 时间口径统一收口

"今日分析"用 `first_analyzed_at`，绝不能用 `analyzed_at`——后者每次分析都会被刷新成当前时间，
重跑一次就把历史条目全算进今天。所有时间范围收口在 `app/core/daterange.py`，
**计数 SQL 与列表 SQL 必须同一口径**，否则会出现"选了今天、计数还是历史总数"。

### 传播画像是弱信号，必须加界

从账号自己的发表记录里学"什么样的标题会被转发"：用**转发率**（分享/阅读）而不是分享绝对数，
避免"读得多所以转得多"的假相关；样本不足时直接返回"不足以判断"，不硬凑；
最终只作为热度的**有界系数**（默认 ±35%），不覆盖热度与时效。

---

## 项目结构

```
app/
├── main.py              # FastAPI 入口：建表 + 路由 + 调度器 + 托管前端
├── pipeline.py          # 流水线编排（fetch/analyze/generate/push/backfill/clean）
├── scheduler.py         # APScheduler，从 schedules 表加载定时任务
├── core/                # 横切：db / schema / config / daterange / checkpoint / 熔断
├── collectors/          # ① 热点抓取（RSS 为主，可扩展 wechat / douyin）
├── analyzer/            # ② 清洗 / 归一化评分 / 聚类 / 传播画像
├── generator/           # ③ 模板库 / 提示词 / LLM / 校验 / 论点 / 标题
├── pusher/              # ④ Playwright 推送 / 排版 / 封面 / 后台只读接口
├── review/              # ⑤ 审核状态机 / 在线编辑 / 批量操作
├── backfill/            # ⑥ 反查发表 + 反馈指标回流
├── dashboard/           # 看板与告警
├── knowledge/           # 热点池清理与审计
└── web/router.py        # 所有业务 API（挂在 /api）

wechat-agent-frontend/   # Vue 3 + Vite（构建产物由后端托管）
tests/                   # pytest
scripts/                 # 启动器
data/                    # 运行时数据（不入库）
```

---

## 安全与隐私

这几样东西**永远不进仓库**（`.gitignore` 已覆盖）：

| 路径 | 为什么 |
|---|---|
| `.env` | LLM API Key |
| `data/app.db` | 生产数据：文章、发表记录、统计 |
| `data/storage_state.json` | **公众号登录会话 cookie**，泄露等于交出账号 |
| `docs/screenshots/`、`*.png` | 截图含真实后台画面 |
| `.workbuddy/` | 内部开发记忆（含个人信息） |

要在新机器上使用推送功能，需要重新扫码登录一次；会话默认 5 天后提示刷新。

---

## 已知限制

- **定时任务目前靠手动触发。** APScheduler 与 `schedules` 表都在，加载逻辑也写好了，
  但还没有配置定时任务的界面——表为空时不会注册任何任务。日常使用是手动点按钮。
- **推送依赖公众号后台的页面结构。** 选择器集中在 `app/pusher/selectors.yaml`，
  后台改版时需要更新；失败时会给出可执行的排查提示，并支持"方案 B"（导出排版好的正文手动粘贴）。
- **部分配置项尚未接入代码**，界面上已明确标注「暂未生效」，不做静默失效。
- **一键启动脚本只覆盖 Windows**（`start_app.bat`）；其他平台用上面的命令行方式启动。

---

## 免责声明

本项目用于自动化**自己账号**的内容生产流程，不包含自动发表或自动群发能力。
使用爬虫/自动化工具时请遵守目标平台的服务条款；本仓库不鼓励也不支持规避平台风控的行为。
