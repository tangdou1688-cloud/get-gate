"""检测 Worker 调用: 并发检查节点、判定出口网络类型."""

from __future__ import annotations

import re
import socket
import ssl
import time
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
    "verified_by": "worker",
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
    """调用 Worker 检测单个节点, 失败按 CHECK_RETRIES 重试; 始终返回结构完整的字典."""
    url = cfg.worker_check_url + quote(f"{node['host']}:{node['port']}", safe="")
    out: dict[str, Any] = dict(node)
    out.update(RESULT_DEFAULTS)
    out["link"] = f"sstp://vpn:vpn@{node['host']}:{node['port']}"
    out["checked_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    attempts = 1 + max(0, cfg.check_retries)

    for attempt in range(attempts):
        retry = attempt + 1 < attempts
        try:
            resp = session.get(
                url, timeout=cfg.check_timeout, headers={"User-Agent": "Mozilla/5.0 (gate-checker)"}
            )
            if resp.status_code != 200:
                out["error"] = f"HTTP {resp.status_code}"
                out["worker_error"] = True
                if retry:
                    time.sleep(1.0)
                    continue
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
            if ok or not retry:
                return out
            time.sleep(1.0)
        except Exception as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"
            out["worker_error"] = True
            if retry:
                time.sleep(1.0)
                continue
            return out
    return out


SSTP_PROBE_PATH = "/sra_{BA195980-CD49-458b-9E23-C84EE0ADCD75}/"


def probe_sstp(host: str, port: int, timeout: float) -> tuple[float, bytes]:
    """直接对节点做 TLS + SSTP_DUPLEX_POST 握手, 返回 (耗时毫秒, 应答前 64 字节)."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.set_ciphers("DEFAULT:@SECLEVEL=0")
    started = time.time()
    with socket.create_connection((host, int(port)), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            request = (
                f"SSTP_DUPLEX_POST {SSTP_PROBE_PATH} HTTP/1.1\r\n"
                f"Host: {host}\r\n"
                "Content-Length: 18446744073709551615\r\n\r\n"
            )
            tls.sendall(request.encode())
            return (time.time() - started) * 1000, tls.recv(64)


def rescue_with_sstp_probe(results: list[dict[str, Any]], cfg: Config) -> int:
    """对 Worker 判失败的节点补一次 SSTP 握手验证.

    原则: 宁可多推不可错杀 —— Worker 的严格判定 (iplocate.io + 12s 门槛) 会误杀
    真实可用的节点, 这里用更宽松的 SSTP 握手作为兜底, 通过即保留.
    """
    if not cfg.sstp_probe:
        return 0
    failed = [item for item in results if not item.get("success")]
    if not failed:
        return 0

    def _try(item: dict[str, Any]) -> float | None:
        try:
            elapsed_ms, response = probe_sstp(item["host"], item["port"], cfg.sstp_probe_timeout)
        except Exception:
            return None
        if b"HTTP/" in response and b" 200" in response:
            return elapsed_ms
        return None

    rescued = 0
    with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
        futures = {pool.submit(_try, item): item for item in failed}
        for future in as_completed(futures):
            item = futures[future]
            elapsed_ms = future.result()
            if elapsed_ms is None:
                continue
            item["success"] = True
            item["status"] = "success"
            item["latency_ms"] = round(elapsed_ms)
            item["error"] = None
            item["verified_by"] = "sstp_probe"
            if item.get("residential") in (None, "unknown"):
                item["residential"] = classify_network(item["host"], None)
            rescued += 1
    return rescued


def check_all(nodes: list[dict[str, Any]], session: requests.Session, cfg: Config) -> list[dict[str, Any]]:
    """并发检测全部节点."""
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
        futures = [pool.submit(check_one, node, session, cfg) for node in nodes]
        for future in as_completed(futures):
            results.append(future.result())
    return results
