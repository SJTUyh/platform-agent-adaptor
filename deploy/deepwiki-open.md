# deepwiki-open 部署与知识库运维（M1）

本文档覆盖：自托管 **deepwiki-open** 起服务 → 本项目对接 → 验证清单 → 备份 / 还原演练。

> 口径说明：deepwiki-open 自身的 LLM / embedder 配置在其**自己的 `.env`** 内，
> 与本项目 `LLM_*` **相互独立**，互不影响。

---

## 一、起服务（自托管 deepwiki-open）

1. 拉取并构建（官方仓库 `AsyncFuncAI/deepwiki-open`）：

   ```bash
   git clone https://github.com/AsyncFuncAI/deepwiki-open.git
   cd deepwiki-open
   # 后端（FastAPI，默认监听 8001）
   pip install -r requirements.txt
   ```

2. 配置 deepwiki-open 自己的 `.env`（**与 issue-master 的 `.env` 是两个文件**）：

   ```dotenv
   # 生成答案用的 LLM（OpenAI 兼容）
   OPENAI_API_KEY=sk-...
   # 向量化（embeddings）；无外网额度可用本地 Ollama
   # OLLAMA_HOST=http://127.0.0.1:11434
   # 服务监听端口（本项目 KB_BASE_URL 默认对接 8001）
   PORT=8001
   ```

3. 启动后端：

   ```bash
   python -m api.main          # 监听 http://127.0.0.1:8001
   # 或： uvicorn api.main:app --host 0.0.0.0 --port 8001
   ```

4. 数据卷位置：deepwiki-open 会把 clone 的仓库、向量库与生成的 wiki 落在其数据目录。
   **将本项目 `KB_DATA_DIR` 指向该目录**，备份 / 还原才能覆盖到「向量库 + wiki」本体
   （默认约定：`./data/deepwiki_open`）。

---

## 二、本项目侧对接

`.env` 关键项（其余见 `.env.example`）：

| 变量 | 值 | 说明 |
| --- | --- | --- |
| `KB_BACKEND` | `deepwiki_open` | 知识库后端 |
| `KB_BASE_URL` | `http://127.0.0.1:8001` | 上面起的服务地址 |
| `KB_PROVIDER` / `KB_MODEL` / `KB_LANGUAGE` | `openai` / 空 / `zh` | 传给 deepwiki-open 的生成参数 |
| `KB_TIMEOUT` | `600` | 首次问答可能触发索引，超时给足 |
| `KB_DATA_DIR` | `./data/deepwiki_open` | 数据卷路径（备份 / 还原对象） |
| `KB_BACKUP_DIR` | `./data/kb_backup` | 归档目录（**建议与数据卷不同盘**） |
| `KB_BACKUP_KEEP` | `7` | 滚动保留份数 |
| `KB_BACKUP_MIN_FREE_GB` | `5` | 磁盘余量阈值，低于则跳过备份并告警 |
| `KB_REFRESH_ENABLED` / `KB_REFRESH_SCHEDULE` | `true` / `03:00` | 每日定时刷新 |
| `KB_BACKUP_ENABLED` / `KB_BACKUP_SCHEDULE` | `true` / `03:30` | 每日定时备份（晚于刷新） |
| `KB_HISTORY_TURNS` | `6` | `/kb ask` 多轮上下文保留轮数（进程内存） |
| `FEISHU_NOTIFY_CHAT` | 群 chat_id | 定时任务结果 / 告警通知群；留空则仅记日志 |

### 端点对接说明（前缀一次性探测）

deepwiki-open 的路由前缀可能是 `/repo/...` 也可能是 `/api/repo/...`（随版本而异）。
`adapters/deepwiki_client.py` 在首次调用时探测一次并**缓存**结果：先试**不带** `/api`，
若返回 404 再试**带** `/api`；两者都不通则记为空前缀（后续调用会如实报错，不伪装成功）。

用到端点（按探测到的前缀拼接）：

| 用途 | 端点 |
| --- | --- |
| 连通性自检 | `GET /health` |
| 前缀探测 / 模型配置 | `GET /models/config` |
| 语言配置 | `GET /lang/config` |
| 触发建索引 | `POST /repo/prepare`（SSE 流） |
| 索引状态 | `GET /repo/index/status` |
| 问答（流式） | `POST /chat/completions/stream`（SSE 流） |
| 已生成 wiki | `GET /api/wiki_cache` |

> `references` / `sources` 出处的**字段名尚未实测确认**，解析按**尽力而为**：
> 命中候选字段（`references` / `sources` / `citations` / `docs` / `context`）则提取，
> 解析不到就返回**空出处列表**并明确显示「出处：未返回出处。」——**绝不编造出处**。

---

## 三、启动自检（本项目）

启动时 `app/main.py` 会打印（仅告警不阻断）：

- `[OK|FAIL] 知识库 KB_BASE_URL`：`probe()` 结果 + 各持有仓库的索引状态摘要（`repo=status@sha`）；
- `[OK|FAIL] 备份目录可写性 / 磁盘余量`：`KB_BACKUP_DIR` 可写 + 剩余空间（低于阈值打标）。

---

## 四、验证清单（M1 验收）

在飞书群里 `@机器人` 后逐条执行，勾选通过：

- [ ] `/kb status` —— 列出持有仓库与索引状态（初次为 `unknown`/无记录）。
- [ ] `/kb refresh AISBench/benchmark` —— 回执「已收到，正在处理…」，随后回报
      `status` / `sha` / 耗时；首次为重建（`fresh`）。
- [ ] 再次 `/kb refresh AISBench/benchmark` —— 仓库无新提交时应回报 **`skipped`**（sha 未变，不重建）。
- [ ] `/kb ask AISBench/benchmark <问题>` —— 返回答案；有出处则列出，无出处显示「出处：未返回出处。」。
- [ ] `/kb ask AISBench/benchmark <追问>` —— 多轮上下文生效（同一群 + 同一仓库）。
- [ ] `/kb backup` —— 产出 `kb_backup_<时间戳>.tar.gz`，回报文件数与体积。
- [ ] `/kb backups` —— 列出备份历史与最近一次结果。
- [ ] 次日观察：到 `KB_REFRESH_SCHEDULE` 自动刷新（sha 未变 → `skipped`）；
      到 `KB_BACKUP_SCHEDULE` 自动备份并按 `KB_BACKUP_KEEP` 滚动清理。
- [ ] 重启进程后 `/kb status` / `/kb backups` 仍能看到索引状态与备份历史（落库持久）。
- [ ] 归档超份数时最旧的自动删除；磁盘余量低于阈值时备份 `skipped` 并告警。

---

## 五、「备份 → 删库 → 还原」演练

> 目的：证明归档**可还原**，而非仅生成成功。**必须在停服状态下执行还原。**
> 下方「实测记录」需在真实环境跑一遍后填录；未实测前**不填**，避免误读为已通过。

### 步骤

1. **停服务**：停止 issue-master 进程（避免还原时写入）。
2. **备份**：

   ```bash
   python scripts/backup_kb.py --create
   python scripts/backup_kb.py --list      # 记下归档文件名
   ```

3. **模拟数据丢失**：删掉数据卷目录（示例，路径以 `KB_DATA_DIR` 为准）：

   ```bash
   # 危险操作，仅演练环境执行
   mv ./data/deepwiki_open ./data/deepwiki_open.deleted
   ```

4. **还原**（脚本会打印确认提示；`--yes` 可跳过）：

   ```bash
   python scripts/backup_kb.py --restore ./data/kb_backup/kb_backup_<时间戳>.tar.gz
   ```

5. **校验**：

   ```bash
   # 数据卷恢复
   ls ./data/deepwiki_open
   # DB 两表恢复（行数应与备份时 manifest 一致）
   python -c "import sqlite3;c=sqlite3.connect('./data/issue_master.db');print('kb_index',c.execute('select count(*) from kb_index').fetchone()[0]);print('kb_cache',c.execute('select count(*) from kb_cache').fetchone()[0])"
   ```

6. 重启 issue-master，`@机器人 /kb status` 应显示与备份前一致的索引状态。

### 实测记录（待填）

| 项 | 值 |
| --- | --- |
| 执行日期 | 待实测 |
| 归档文件 / 体积 | 待实测 |
| 归档内 manifest 文件数 | 待实测 |
| kb_index / kb_cache 备份行数 | 待实测 |
| 还原后 kb_index / kb_cache 行数 | 待实测 |
| 还原后 `/kb status` 是否恢复 | 待实测 |
| 结论 | 待实测 |

---

## 六、注意事项

- 备份目录 `KB_BACKUP_DIR` 建议与数据卷 `KB_DATA_DIR` **不同盘 / 不同挂载**，避免同盘故障一起丢。
- 同一时刻只允许一个刷新任务（全局锁）；刷新进行中再次 `/kb refresh` 会回报 `busy`。
- 刷新期间的 `ask` 走缓存或按现状应答（索引非 `fresh` 时答案前会加 ⚠️ 提示，不静默）。
- deepwiki-open 首次索引会 clone + 向量化，耗时长；建议部署后先 `/kb refresh` 预热。

---

## 七、本地补丁：只检索端点 `/retrieval`

为支持平台 Agent 接入（issue-master `/api/v1/kb/search`），本机 deepwiki-open 增加了一个**只新增、不改既有逻辑**的补丁：

- 新增 `api/routers/retrieval.py`：`POST /retrieval`
  - 入参：`repo_url`、`type`、`query`、`top_k`（1~12）、`provider`、`model`、`token?`
  - 复用 `api.services.research.prepare_repo_index()` 加载已建好的索引，并按 `(repo_url,type,provider,model)` 进程内缓存 `RAG`，避免每次检索重新加载 FAISS
  - 调 `rag.call(query)` 取 `documents`，返回 `{repo, chunks:[{path,startLine,endLine,title,text,score}]}`，单个片段截断 1200 字符
  - 未索引返回 425，其余错误返回 500
- `api/main.py` 仅两处一行改动：导入 `retrieval` 并加入 `app.include_router` 列表

注意：`provider`/`model` 必须与建索引时一致（embedding 维度不同会报错）；issue-master 会透传 `KB_PROVIDER` / `KB_MODEL`。

上游升级后需重新应用该补丁（重放上述文件改动），索引数据不受影响。
