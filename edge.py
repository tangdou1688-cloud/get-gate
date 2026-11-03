"""优选入口: 静态优选域名 + 优选 API 端点合并."""

from __future__ import annotations

import ipaddress
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter

from config import Config, split_list
from logsetup import get

log = get("OPTIMAL API")
log_pool = get("EDGE POOL")

_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.I
)
_PORT_RE = re.compile(r"^\d{1,5}$")
_JSON_KEYS = ("ip", "host", "address", "server", "hostname", "ip_address")


class EdgePoolError(RuntimeError):
    """入口池为空, 无法生成节点."""


def safe_url(url: str) -> str:
    """日志用地址: 去掉 query/fragment, 避免 key=... 进公开日志."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """IPv4/IPv6 统一过滤: 未指定/私网/回环/组播/保留/链路本地."""
    return not (
        ip.is_unspecified
        or ip.is_private
        or ip.is_loopback
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_link_local
    )


def normalize_endpoint(token: str) -> str | None:
    """校验并规范单个端点: ip / ip:port / [ipv6]:port / 域名:port.

    非法、内网、CIDR、越界端口一律返回 None.
    """
    token = token.strip().strip("\"'`").rstrip(".")
    if not token or "/" in token or len(token) > 260:
        return None

    host, port = token, "443"
    if token.startswith("["):
        matched = re.fullmatch(r"\[([0-9a-fA-F:.]+)\](?::(\d{1,5}))?", token)
        if not matched:
            return None
        host, port = matched.group(1), matched.group(2) or "443"
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return None
        if not _is_public_ip(ip):
            return None
        host = f"[{host}]"
    else:
        if token.count(":") > 1:
            return None  # 裸 IPv6 无法与端口区分, 要求加方括号
        if token.count(":") == 1:
            host, port = token.split(":")
            if port and not _PORT_RE.match(port):
                return None
            port = port or "443"
        try:
            ip = ipaddress.ip_address(host)
            if not _is_public_ip(ip):
                return None
        except ValueError:
            if not _HOSTNAME_RE.match(host) or host != host.lower():
                return None

    if not (1 <= int(port) <= 65535):
        return None
    return f"{host}:{port}"


def _collect_json_endpoints(obj: object, out: list[str]) -> None:
    """递归提取 JSON 中的 ip/host/address 字段或字符串端点."""
    if isinstance(obj, dict):
        for key in _JSON_KEYS:
            value = obj.get(key)
            if isinstance(value, str):
                endpoint = normalize_endpoint(value)
                if endpoint:
                    out.append(endpoint)
        for value in obj.values():
            if isinstance(value, (dict, list)):
                _collect_json_endpoints(value, out)
    elif isinstance(obj, list):
        for value in obj:
            if isinstance(value, str):
                endpoint = normalize_endpoint(value)
                if endpoint:
                    out.append(endpoint)
            elif isinstance(value, (dict, list)):
                _collect_json_endpoints(value, out)


def parse_optimal_payload(text: str) -> list[str]:
    """解析优选 API 返回内容.

    兼容纯文本 (`1.2.3.4`、`1.2.3.4:2053`、`1.2.3.4#备注`、逗号分隔) 与
    JSON (`{"info":[{"ip":"1.2.3.4","colo":"HKG"}]}`). 返回未去重的端点列表.
    """
    text = (text or "").strip()
    if not text:
        return []

    if text[0] in "{[":
        try:
            found: list[str] = []
            _collect_json_endpoints(json.loads(text), found)
            return found
        except (json.JSONDecodeError, ValueError):
            pass

    if text.lstrip().startswith("<"):  # HTML 错误页 / 反爬页面
        return []

    found = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        for token in re.split(r"[,\s]+", line):
            endpoint = normalize_endpoint(token)
            if endpoint:
                found.append(endpoint)
    return found


def _make_session(size: int) -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=size, pool_maxsize=size)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _fetch_one(url: str, timeout: float, session: requests.Session) -> list[str]:
    resp = session.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
    if resp.status_code != 200:
        log.info("API 返回状态码异常 (%s): %s", resp.status_code, safe_url(url))
        return []
    return parse_optimal_payload(resp.text)


def fetch_optimal_api_endpoints(apis: list[str], timeout: float) -> list[str]:
    """并发拉取所有优选 API, 按配置顺序合并去重."""
    urls = [url for url in apis if url]
    if not urls:
        return []

    results: list[list[str]] = [[] for _ in urls]
    session = _make_session(max(1, min(8, len(urls))))
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(urls)))) as pool:
        futures = {}
        for index, url in enumerate(urls):
            log.info("请求优选 API: %s", safe_url(url))
            futures[pool.submit(_fetch_one, url, timeout, session)] = (index, url)
        for future in as_completed(futures):
            index, url = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:
                log.info("拉取优选 API 失败 (%s): %s: %s", safe_url(url), type(exc).__name__, exc)
    session.close()

    endpoints: list[str] = []
    seen: set[str] = set()
    for found in results:
        added = 0
        for endpoint in found:
            if endpoint in seen:
                continue
            seen.add(endpoint)
            endpoints.append(endpoint)
            added += 1
        log.info("解析到 %d 个端点, 去重后新增 %d 个", len(found), added)
    return endpoints


def static_hosts(cfg: Config) -> list[str]:
    """静态优选域名: HOSTS_ENTRY / EDGE_HOSTS 覆盖, 再追加 EDGE_HOSTS_EXTRA."""
    base = split_list(cfg.hosts_entry) if cfg.hosts_entry else list(cfg.edge_hosts)
    return base + list(cfg.edge_hosts_extra)


def build_edge_pool(cfg: Config) -> list[str]:
    """合并静态优选域名与优选 API 端点, 返回去重后的入口列表."""
    hosts = static_hosts(cfg)
    api_hosts = fetch_optimal_api_endpoints(cfg.optimal_apis, cfg.optimal_api_timeout)

    combined: list[str] = []
    seen: set[str] = set()
    for host in hosts + api_hosts:
        if not host or host in seen:
            continue
        seen.add(host)
        combined.append(host)

    log_pool.info(
        "汇总入口地址池: 静态域名 %d 个, API 获取 %d 个, 合并有效入口共 %d 个",
        len(hosts),
        len(api_hosts),
        len(combined),
    )
    if not combined:
        raise EdgePoolError("优选入口池为空: 静态域名与优选 API 均未提供有效入口, 无法生成 nodes.txt")
    return combined
