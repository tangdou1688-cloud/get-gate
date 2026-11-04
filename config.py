"""配置加载: 只读取环境变量与数据文件, 不在 import 时执行解析.

对外行为保持与旧版一致:
- 环境变量名不变 (EDGE_HOSTS / EDGE_HOSTS_EXTRA / HOSTS_ENTRY / OPTIMAL_API* / SECRET_KEY ...)
- 默认静态域名与默认优选 API 移入 edge_hosts.txt / optimal_apis.txt
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Mapping

DEFAULT_SITE_URL = "https://tangdou1688-cloud.github.io/get-gate"
SECRET_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_SECTION_RE = re.compile(r"^#\s*section\s*:\s*(ct|cu|cmcc|extra)\s*$", re.I)


class ConfigError(ValueError):
    """配置非法, 调用方应终止运行."""


@dataclass(frozen=True)
class Config:
    """一次运行所需的全部配置 (不可变)."""

    repo_dir: str
    public_dir: str
    template_html: str
    edge_hosts_file: str
    optimal_apis_file: str

    vpngate_api: str
    vpngate_mirror: str
    http_timeout: int

    worker_check_url: str = field(repr=False)
    concurrency: int
    check_timeout: float
    max_check_nodes: int
    check_retries: int
    sstp_probe: bool
    sstp_probe_timeout: float
    check_samples: int
    purity_check: bool
    purity_timeout: float
    purity_max_new: int
    node_blacklist_fails: int

    optimal_apis: list[str] = field(default_factory=list)
    optimal_api_timeout: float = 15.0

    edge_hosts: list[str] = field(default_factory=list)
    edge_hosts_extra: list[str] = field(default_factory=list)
    edge_ports: list[int] = field(default_factory=lambda: [443])
    hosts_entry: str = ""

    secret_key: str = field(default="", repr=False)
    site_url: str = DEFAULT_SITE_URL
    nodes_url: str = ""
    previous_data_url: str = ""
    blacklist_url: str = ""
    blacklist_file: str = ""

    def validate(self) -> None:
        """校验必须项, 失败抛 ConfigError (对应旧版的 die 条件)."""
        if not self.worker_check_url:
            raise ConfigError(
                "未配置检测 Worker: 请设置环境变量 CHECK_WORKER (本地) 或 GitHub Actions Secret DOMAIN"
            )
        if self.secret_key and not SECRET_KEY_RE.fullmatch(self.secret_key):
            raise ConfigError("SECRET_KEY 非法: 仅允许 A-Z a-z 0-9 _ -, 长度 16-64")


def split_list(*values: str) -> list[str]:
    """把逗号/空白分隔的配置拆成去重列表 (保持顺序)."""
    items: list[str] = []
    for value in values:
        for item in re.split(r"[\s,]+", value or ""):
            if item and item not in items:
                items.append(item)
    return items


def _parse_ports(raw: str) -> list[int]:
    """解析 EDGE_PORTS (逗号/空白分隔), 非法值忽略; 空则回退 443."""
    ports: list[int] = []
    for item in re.split(r"[\s,]+", raw or ""):
        if not item:
            continue
        try:
            port = int(item)
        except ValueError:
            continue
        if 1 <= port <= 65535 and port not in ports:
            ports.append(port)
    return ports or [443]


def read_list_file(path: str) -> list[str]:
    """读取一行一个的列表文件, 忽略空行与 # 注释."""
    items: list[str] = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                items.append(line)
    except FileNotFoundError:
        return []
    return items


def read_api_file(path: str) -> dict[str, list[str]]:
    """读取优选 API 文件, 支持 `# section: ct|cu|cmcc|extra` 分组."""
    sections: dict[str, list[str]] = {}
    current = "extra"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                if line.startswith("#"):
                    matched = _SECTION_RE.match(line)
                    if matched:
                        current = matched.group(1).lower()
                    continue
                sections.setdefault(current, []).append(line)
    except FileNotFoundError:
        return {}
    return sections


def _env_value(env: Mapping[str, str], key: str, default: str = "") -> str:
    return (env.get(key) or default).strip()


def load_config(env: Mapping[str, str] | None = None, repo_dir: str | None = None) -> Config:
    """从环境变量与数据文件构建配置 (供 main 调用)."""
    env = os.environ if env is None else env
    repo_dir = repo_dir or os.path.dirname(os.path.abspath(__file__))

    edge_hosts_file = os.path.join(repo_dir, "edge_hosts.txt")
    optimal_apis_file = os.path.join(repo_dir, "optimal_apis.txt")

    default_hosts = read_list_file(edge_hosts_file)
    hosts_raw = env.get("EDGE_HOSTS")
    edge_hosts = split_list(hosts_raw) if hosts_raw is not None else default_hosts

    api_sections = read_api_file(optimal_apis_file)
    legacy_api = _env_value(env, "OPTIMAL_API")
    if legacy_api:
        optimal_apis = split_list(legacy_api)
    else:
        groups: list[str] = []
        for section in ("ct", "cu", "cmcc", "extra"):
            raw = env.get(f"OPTIMAL_API_{section.upper()}")
            groups.extend(split_list(raw) if raw is not None else api_sections.get(section, []))
        optimal_apis = split_list(",".join(groups))

    secret_key = _env_value(env, "SECRET_KEY")
    site_url = _env_value(env, "SITE_URL", DEFAULT_SITE_URL).rstrip("/")
    nodes_url = _env_value(env, "NODES_URL") or (
        f"{site_url}/{(secret_key + '/') if secret_key else ''}nodes.txt"
    )
    previous_data_url = _env_value(env, "PREVIOUS_DATA_URL") or (
        f"{site_url}/{(secret_key + '/') if secret_key else ''}data.json"
    )

    return Config(
        repo_dir=repo_dir,
        public_dir=_env_value(env, "PUBLIC_DIR") or os.path.join(repo_dir, "public"),
        template_html=os.path.join(repo_dir, "web", "index.html"),
        edge_hosts_file=edge_hosts_file,
        optimal_apis_file=optimal_apis_file,
        vpngate_api=_env_value(env, "VPNGATE_API", "https://www.vpngate.net/api/iphone/"),
        vpngate_mirror=_env_value(
            env,
            "VPNGATE_MIRROR",
            "https://raw.githubusercontent.com/fdciabdul/Vpngate-Scraper-API/main/json/data.json",
        ),
        http_timeout=int(_env_value(env, "HTTP_TIMEOUT", "60")),
        worker_check_url=_env_value(env, "CHECK_WORKER"),
        concurrency=max(1, int(_env_value(env, "CHECK_CONCURRENCY", "32"))),
        check_timeout=float(_env_value(env, "CHECK_TIMEOUT", "90")),
        max_check_nodes=int(_env_value(env, "MAX_CHECK_NODES", "0")),
        check_retries=max(0, int(_env_value(env, "CHECK_RETRIES", "1"))),
        sstp_probe=_env_value(env, "SSTP_PROBE", "1").lower() not in ("0", "false", "no", "off"),
        sstp_probe_timeout=float(_env_value(env, "SSTP_PROBE_TIMEOUT", "8")),
        check_samples=min(5, max(1, int(_env_value(env, "CHECK_SAMPLES", "1")))),
        purity_check=_env_value(env, "PURITY_CHECK", "1").lower() not in ("0", "false", "no", "off"),
        purity_timeout=float(_env_value(env, "PURITY_TIMEOUT", "10")),
        purity_max_new=max(0, int(_env_value(env, "PURITY_MAX_NEW", "100"))),
        node_blacklist_fails=max(0, int(_env_value(env, "NODE_BLACKLIST_FAILS", "5"))),
        optimal_apis=optimal_apis,
        optimal_api_timeout=float(_env_value(env, "OPTIMAL_API_TIMEOUT", "15")),
        edge_hosts=edge_hosts,
        edge_hosts_extra=split_list(_env_value(env, "EDGE_HOSTS_EXTRA")),
        edge_ports=_parse_ports(_env_value(env, "EDGE_PORTS", "443")),
        hosts_entry=_env_value(env, "HOSTS_ENTRY"),
        secret_key=secret_key,
        site_url=site_url,
        nodes_url=nodes_url,
        previous_data_url=previous_data_url,
        blacklist_url=_env_value(env, "BLACKLIST_URL"),
        blacklist_file=_env_value(env, "BLACKLIST_FILE") or os.path.join(repo_dir, "blacklist.txt"),
    )


def mask_secret(text: str, secret: str) -> str:
    """日志脱敏: 把密钥替换为 ****."""
    if not secret:
        return text
    return text.replace(secret, "****")
