"""集中配置：读取 .env / 环境变量，暴露一个全局 `Config`。

工程约定对齐参考项目 review_agent：自带极简 .env 解析（不覆盖已存在的环境变量），
`Config.validate()` 供启动自检调用。

注意：`LLM_*` / `ISSUE_DIGEST_*` / `GITHUB_REPOS` / `KB_*` 里带单行配置表的部分，
**首启播种进 DB 之后以 DB 为准**（飞书在线改的就是 DB），本文件的值只在首次运行时生效。
"""
import os
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    """极简 .env 解析：KEY=VALUE，忽略空行与 # 注释，不覆盖已存在的环境变量。"""
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            # 引号包裹：内容原样保留（其中的 # 不算注释）
            value = value[1:-1]
        elif " #" in value:
            # 行内注释：以「空白 + #」起头截断（dotenv 惯例）
            value = value.split(" #", 1)[0].strip()
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key) or default)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_list(key: str, default: str = "") -> list[str]:
    return [x.strip() for x in _env(key, default).split(",") if x.strip()]


class Config:
    # --- GitHub（回写身份：GitHub App 推荐，PAT 兜底）---
    # app（GitHub App，推荐）/ pat（快速启动）
    github_auth_mode: str = _env("GITHUB_AUTH_MODE", "app")
    # app 模式：用于签发 installation token（1h，自动轮换）
    github_app_id: str = _env("GITHUB_APP_ID")
    github_app_private_key_path: str = _env("GITHUB_APP_PRIVATE_KEY_PATH")
    github_app_installation_id: str = _env("GITHUB_APP_INSTALLATION_ID")
    # pat 模式：机器人 PAT（fine-grained：Issues RW + Contents R + Metadata R）
    github_bot_token: str = _env("GITHUB_BOT_TOKEN")
    # 持有/巡检的仓库列表（首启播种进 DB，之后以 DB 为准）；必填
    github_repos: list[str] = _env_list("GITHUB_REPOS", "AISBench/benchmark")
    # 单仓库命令缺省目标（/kb refresh、/issue handle 等）
    github_default_repo: str = _env("GITHUB_DEFAULT_REPO", "AISBench/benchmark")
    github_api_base: str = _env("GITHUB_API_BASE", "https://api.github.com")

    # --- issue 巡检与汇报 ---
    # 默认回溯天数（首启播种进 digest_config）
    issue_digest_days: int = _env_int("ISSUE_DIGEST_DAYS", 1)
    # 每日定时 HH:MM，off 关闭（首启播种进 digest_config）
    issue_digest_schedule: str = _env("ISSUE_DIGEST_SCHEDULE", "09:30")
    # 判定"停滞"的无动向天数
    issue_stall_days: int = _env_int("ISSUE_STALL_DAYS", 7)

    # --- 知识库（自托管 deepwiki-open）---
    kb_backend: str = _env("KB_BACKEND", "deepwiki_open")
    kb_base_url: str = _env("KB_BASE_URL", "http://127.0.0.1:8001")
    kb_provider: str = _env("KB_PROVIDER", "openai")
    kb_model: str = _env("KB_MODEL")
    kb_language: str = _env("KB_LANGUAGE", "zh")
    kb_timeout: int = _env_int("KB_TIMEOUT", 600)
    # 每日定时刷新（首启播种进 kb_config）
    kb_refresh_enabled: bool = _env_bool("KB_REFRESH_ENABLED", True)
    kb_refresh_schedule: str = _env("KB_REFRESH_SCHEDULE", "03:00")
    # 每日定时备份、保留份数
    kb_backup_enabled: bool = _env_bool("KB_BACKUP_ENABLED", True)
    kb_backup_schedule: str = _env("KB_BACKUP_SCHEDULE", "03:30")
    kb_backup_keep: int = _env_int("KB_BACKUP_KEEP", 7)
    # 备份归档目录（建议与数据卷不同盘）
    kb_backup_dir: str = _env("KB_BACKUP_DIR", "./data/kb_backup")
    # 磁盘剩余阈值（GB），低于则跳过备份并告警
    kb_backup_min_free_gb: int = _env_int("KB_BACKUP_MIN_FREE_GB", 5)
    # /kb ask 多轮上下文保留轮数（进程内存，重启重置）
    kb_history_turns: int = _env_int("KB_HISTORY_TURNS", 6)
    # deepwiki-open 数据卷路径（备份/还原对象；应与 KB_BACKUP_DIR 不同盘）
    kb_data_dir: str = _env("KB_DATA_DIR", "./data/deepwiki_open")

    # --- Agent 层（所有模型调用的唯一入口）---
    # 选用后端；内置：echo（pydantic_ai 属 M1 起启用）
    agent_backend: str = _env("AGENT_BACKEND", "pydantic_ai")
    # 后端不可用时的降级链（逗号分隔，按序下探）
    agent_fallback: list[str] = _env_list("AGENT_FALLBACK", "echo")
    agent_tools_enabled: bool = _env_bool("AGENT_TOOLS_ENABLED", True)
    agent_max_turns: int = _env_int("AGENT_MAX_TURNS", 8)
    # 可选 MCP server（JSON 数组），留空则不挂载（含官方 DeepWiki MCP）
    agent_mcp_servers: str = _env("AGENT_MCP_SERVERS")

    # --- LLM（OpenAI 兼容端点；首启播种进 llm_config，之后以 DB 为准）---
    llm_base_url: str = _env("LLM_BASE_URL", "https://api.openai.com/v1")
    llm_api_key: str = _env("LLM_API_KEY")
    llm_model: str = _env("LLM_MODEL", "gpt-4o-mini")
    llm_timeout: int = _env_int("LLM_TIMEOUT", 600)

    # --- 平台接入 HTTP API（默认关闭）---
    # 兼容旧 issue_master 的 ISSUE_MASTER_API_* 环境变量。
    api_enabled: bool = _env_bool("ADAPTOR_API_ENABLED", False) or _env_bool("ISSUE_MASTER_API_ENABLED", False)
    # 监听地址：默认仅回环；跨主机部署改 0.0.0.0 或内网地址
    api_host: str = _env("ADAPTOR_API_HOST") or _env("ISSUE_MASTER_API_HOST", "127.0.0.1")
    api_port: int = _env_int("ADAPTOR_API_PORT", _env_int("ISSUE_MASTER_API_PORT", 8091))
    # 共享令牌：启用 API 时必填
    api_token: str = _env("ADAPTOR_API_TOKEN") or _env("ISSUE_MASTER_API_TOKEN")
    # 可选：直接 TLS 监听（留空走明文，建议反向代理终止 TLS）
    api_tls_cert: str = _env("ADAPTOR_API_TLS_CERT") or _env("ISSUE_MASTER_API_TLS_CERT")
    api_tls_key: str = _env("ADAPTOR_API_TLS_KEY") or _env("ISSUE_MASTER_API_TLS_KEY")
    # 每分钟最大请求数
    api_rate_limit: int = _env_int("ADAPTOR_API_RATE_LIMIT", _env_int("ISSUE_MASTER_API_RATE_LIMIT", 30))

    # --- 本服务 ---
    fernet_key: str = _env("FERNET_KEY")
    db_path: str = _env("DB_PATH", "./data/agent-adaptor.db")
    # 任务队列 worker 线程数（内部 LLM 草稿生成 / KB 备份）
    queue_workers: int = _env_int("QUEUE_WORKERS", 2)
    # 自定义 CA bundle（例如代理的中间人自签名根证书）；留空则用系统默认
    ca_bundle: str = _env("CA_BUNDLE")

    @classmethod
    def validate(cls) -> list[str]:
        """返回缺失的关键配置项列表，供启动时自检。"""
        missing: list[str] = []
        mode = (cls.github_auth_mode or "app").lower()
        if mode == "pat":
            if not cls.github_bot_token:
                missing.append("GITHUB_BOT_TOKEN")
        else:
            if not cls.github_app_id:
                missing.append("GITHUB_APP_ID")
            if not cls.github_app_private_key_path:
                missing.append("GITHUB_APP_PRIVATE_KEY_PATH")
            if not cls.github_app_installation_id:
                missing.append("GITHUB_APP_INSTALLATION_ID")
        if not cls.github_repos:
            missing.append("GITHUB_REPOS")
        if not cls.fernet_key:
            missing.append("FERNET_KEY")
        return missing


# 让 requests / ssl 信任自定义 CA（如代理的中间人根证书），需在其他库发起请求前设置。
if Config.ca_bundle:
    _ca = os.path.abspath(Config.ca_bundle)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _ca)
    os.environ.setdefault("SSL_CERT_FILE", _ca)

# 代理绕过名单（两类，都必须在代理开启时自动注入 no_proxy / NO_PROXY）：
#   1) GitHub 域名：实测本机代理（gost）对 api.github.com 会 TLS 握手超时；
#   2) 回环地址：本地服务（如 deepwiki-open :8001）绝不能走代理——否则代理会
#      一直等（ReadTimeout）而不是立刻 Connection refused，导致自检卡到超时。
# 保留用户已有配置，仅追加缺失项。
_NO_PROXY_HOSTS = (
    "api.github.com",
    "github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "localhost",
    "127.0.0.1",
    "::1",
)


def _ensure_no_proxy_hosts() -> None:
    proxy_on = any(
        os.environ.get(k)
        for k in (
            "HTTP_PROXY",
            "http_proxy",
            "HTTPS_PROXY",
            "https_proxy",
            "ALL_PROXY",
            "all_proxy",
        )
    )
    if not proxy_on:
        return
    for var in ("no_proxy", "NO_PROXY"):
        hosts = [x.strip() for x in os.environ.get(var, "").split(",") if x.strip()]
        for host in _NO_PROXY_HOSTS:
            if host not in hosts:
                hosts.append(host)
        os.environ[var] = ",".join(hosts)


_ensure_no_proxy_hosts()
