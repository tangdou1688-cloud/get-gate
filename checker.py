"""检测 Worker 调用: 并发检查节点、判定出口网络类型."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter

from config import Config
from logsetup import get

log = get("CLOUDFLARE WORKER")

DATA_CENTER_ORG_KEYWORDS = [
    "GOOGLE", "AMAZON", "AWS", "MICROSOFT", "OVH", "HETZNER", "DIGITALOCEAN",
    "AKAMAI", "CLOUDFLARE", "FASTLY", "RACKSPACE", "EQUINIX", "LINODE", "VULTR",
    "HURRICANE", "TENCENT", "ALIBABA", "ALIYUN", "LEASWEB",
]

RESIDENTIAL_ORG_KEYWORDS = [
    "NTT EAST", "NTT WEST", "NTT COMMUNICATIONS", "NTT BROADBAND", "KDDI", "DOCOMO",
    "SOFTBANK", "AU COMMUNICATIONS", "J:COM", "JCOM", "OCN", "BIGLOBE",
    "IIJ", "SEIKO", "CLEVER-NET", "AT&T", "COMCAST", "XFINITY", "VERIZON",
    "TELUS", "ROGERS", "BELL CANADA", "VODAFONE", "ORANGE", "DEUTSCHE TELEKOM",
    "BREEZE", "TIM S.P.A", "LIBERO", "FASTWEB", "FREE FRANCE", "BT OPEN",
]

RESULT_DEFAULTS: dict[str, Any] = {
    "protocol": "sstp",
    "status": "failed",
    "success": False,
    "latency_ms": None,
    "colo": None,
    "exit": None,
    "residential": "unknown",
    "error": None,
    "worker_error": False,
}


def make_session(concurrency: int) -> requests.Session:
    """创建共享 Session, 连接池大小与并发数对齐 (避免默认 10 条连接瓶颈)."""
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=concurrency, pool_maxsize=concurrency)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _org_matches(org: str, keywords: Iterable[str]) -> bool:
    """按词边界匹配运营商关键词, 避免 OCN 命中 DOCN 之类的误伤."""
    for keyword in keywords:
        pattern = rf"(?<![A-Z0-9]){re.escape(keyword)}(?![A-Z0-9])"
        if re.search(pattern, org):
            return True
    return False


def classify_network(host: str, exit_org: str | None, is_datacenter: bool | None = None) -> str:
    """判定出口网络类型: datacenter / residential / unknown.

    注意: residential 是基于 ASN 组织名与主机名的估计值, 不是权威判定.
    """
    if is_datacenter is True:
        return "datacenter"
    if is_datacenter is False:
        return "residential"
    org = (exit_org or "").upper()
    if org:
        if _org_matches(org, DATA_CENTER_ORG_KEYWORDS):
            return "datacenter"
        if _org_matches(org, RESIDENTIAL_ORG_KEYWORDS):
            return "residential"
    host = host.lower()
    if host.startswith("public-vpn"):
        return "datacenter"
    if re.match(r"^vpn\d{5,}", host) or re.match(r"^vpnv\d+", host):
        return "residential"
    return "unknown"


def check_one(node: dict[str, Any], session: requests.Session, cfg: Config) -> dict[str, Any]:
    """调用 Worker 检测单个节点, 始终返回结构完整的字典."""
    url = cfg.worker_check_url + quote(f"{node['host']}:{node['port']}", safe="")
    out: dict[str, Any] = dict(node)
    out.update(RESULT_DEFAULTS)
    out["link"] = f"sstp://vpn:vpn@{node['host']}:{node['port']}"
    out["checked_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        resp = session.get(
            url, timeout=cfg.check_timeout, headers={"User-Agent": "Mozilla/5.0 (gate-checker)"}
        )
        if resp.status_code != 200:
            out["error"] = f"HTTP {resp.status_code}"
            out["worker_error"] = True
            return out
        payload = resp.json()
        ok = bool(payload.get("success"))
        out["success"] = ok
        out["status"] = "success" if ok else "failed"
        out["latency_ms"] = payload.get("responseTime")
        out["colo"] = payload.get("colo")
        out["error"] = None if ok else (payload.get("error") or payload.get("message") or "check failed")
        exit_info = payload.get("exit") or {}
        if exit_info:
            asn = exit_info.get("asn") or {}
            org = asn.get("org") or asn.get("name") or ""
            out["exit"] = {
                "ip": exit_info.get("ip"),
                "country": exit_info.get("country"),
                "country_code": exit_info.get("country_code"),
                "city": exit_info.get("city"),
                "continent": exit_info.get("continent"),
                "asn": asn.get("asn"),
                "org": org,
                "type": asn.get("type"),
                "is_datacenter": exit_info.get("is_datacenter"),
            }
            out["residential"] = classify_network(
                out["host"], org, exit_info.get("is_datacenter")
            )
        else:
            out["residential"] = classify_network(out["host"], None, None)
        return out
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["worker_error"] = True
        return out


def check_all(nodes: list[dict[str, Any]], session: requests.Session, cfg: Config) -> list[dict[str, Any]]:
    """并发检测全部节点."""
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
        futures = [pool.submit(check_one, node, session, cfg) for node in nodes]
        for future in as_completed(futures):
            results.append(future.result())
    return results
