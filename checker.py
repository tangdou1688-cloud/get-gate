"""检测 Worker 调用: 并发检查节点、判定出口网络类型."""

from __future__ import annotations

import re
import socket
import ssl
import statistics
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


def _apply_exit(out: dict[str, Any], payload: dict[str, Any]) -> None:
    """把 Worker 返回的出口信息写入结果字典."""
    exit_info = payload.get("exit") or {}
    if not exit_info:
        out["residential"] = classify_network(out["host"], None, None)
        return
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
    out["residential"] = classify_network(out["host"], org, exit_info.get("is_datacenter"))


def check_one(node: dict[str, Any], session: requests.Session, cfg: Config) -> dict[str, Any]:
    """调用 Worker 检测单个节点.

    成功时按 CHECK_SAMPLES 采样多次, 取中位数作为延迟 (单次测量抖动大);
    失败按 CHECK_RETRIES 重试. 始终返回结构完整的字典.
    """
    url = cfg.worker_check_url + quote(f"{node['host']}:{node['port']}", safe="")
    out: dict[str, Any] = dict(node)
    out.update(RESULT_DEFAULTS)
    out["link"] = f"sstp://vpn:vpn@{node['host']}:{node['port']}"
    out["checked_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    samples: list[float] = []
    failures = 0
    max_rounds = max(cfg.check_samples, 1 + max(0, cfg.check_retries))
    for _ in range(max_rounds):
        try:
            resp = session.get(
                url, timeout=cfg.check_timeout, headers={"User-Agent": "Mozilla/5.0 (gate-checker)"}
            )
            if resp.status_code != 200:
                failures += 1
                out["worker_error"] = True
                out["error"] = f"HTTP {resp.status_code}"
            else:
                payload = resp.json()
                out["colo"] = payload.get("colo")
                _apply_exit(out, payload)
                if payload.get("success"):
                    samples.append(float(payload.get("responseTime") or 0.0))
                    out["error"] = None
                    if len(samples) >= cfg.check_samples:
                        break
                else:
                    failures += 1
                    out["error"] = payload.get("error") or payload.get("message") or "check failed"
        except Exception as exc:
            failures += 1
            out["worker_error"] = True
            out["error"] = f"{type(exc).__name__}: {exc}"
        if failures > cfg.check_retries:
            break
        time.sleep(0.5)

    if samples:
        out["success"] = True
        out["status"] = "success"
        out["latency_ms"] = round(statistics.median(samples))
        out["latency_samples"] = [round(value) for value in samples]
    else:
        out["success"] = False
        out["status"] = "failed"
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


def probe_latencies(results: list[dict[str, Any]], cfg: Config) -> None:
    """对所有节点做一次独立的 SSTP 握手测速 (不依赖 Worker 的 iplocate 检测).

    测得的 probe_latency_ms 是 TLS+SSTP 握手 RTT, 与 Worker 的全链耗时口径不同,
    但同口径可比, 用来给节点命名/排序.
    """
    if not cfg.sstp_probe or not results:
        return

    def _one(item: dict[str, Any]) -> int | None:
        try:
            elapsed_ms, response = probe_sstp(item["host"], item["port"], cfg.sstp_probe_timeout)
        except Exception:
            return None
        if b"HTTP/" in response and b" 200" in response:
            return round(elapsed_ms)
        return None

    with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
        futures = {pool.submit(_one, item): item for item in results}
        for future in as_completed(futures):
            item = futures[future]
            probe_ms = future.result()
            if probe_ms is not None:
                item["probe_latency_ms"] = probe_ms


def apply_probe_results(results: list[dict[str, Any]]) -> int:
    """用独立测速结果覆盖命名延迟; Worker 判失败但握手通过的节点予以保留."""
    rescued = 0
    for item in results:
        probe_ms = item.get("probe_latency_ms")
        if probe_ms is None:
            continue
        if item.get("success"):
            item["worker_latency_ms"] = item.get("latency_ms")
            item["latency_ms"] = probe_ms
            continue
        item["success"] = True
        item["status"] = "success"
        item["latency_ms"] = probe_ms
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
