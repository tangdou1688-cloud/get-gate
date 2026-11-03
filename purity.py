"""IP 纯净度综合评分: 多来源打分后合成 0-100 的纯净度.

来源 (均不依赖 key):
  - Scamalytics 网页 Fraud Score (0-100, 越高越脏)
  - proxycheck.io 免费接口 risk (0-100) + proxy 标记
  - ip-api.com 免费接口 proxy/hosting 标记 (仅 IPv4, HTTP)

合成: purity = 100 - 平均风险 - 标记惩罚, 结果夹在 0-100.
只对新出现的出口 IP 查询; 历史 IP 复用上一轮 data.json 里的分数.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

from config import Config
from logsetup import get

log = get("PURITY")
UA = {"User-Agent": "Mozilla/5.0 (compatible; gate-purity/1.0)"}
_FRAUD_RE = re.compile(r"Fraud Score:\s*(\d{1,3})", re.I)


def _scamalytics(ip: str, session: requests.Session, timeout: float) -> int | None:
    try:
        resp = session.get(f"https://scamalytics.com/ip/{ip}", timeout=timeout, headers=UA)
        if resp.status_code != 200:
            return None
        matched = _FRAUD_RE.search(resp.text)
        return int(matched.group(1)) if matched else None
    except Exception:
        return None


def _proxycheck(ip: str, session: requests.Session, timeout: float) -> dict[str, Any] | None:
    try:
        resp = session.get(
            f"https://proxycheck.io/v2/{ip}?vpn=1&asn=1&risk=1", timeout=timeout, headers=UA
        )
        payload = resp.json()
        data = payload.get(ip) or {}
        risk = data.get("risk")
        return {
            "risk": int(risk) if isinstance(risk, (int, float)) else None,
            "proxy": data.get("proxy") == "yes",
        }
    except Exception:
        return None


def _ipapi(ip: str, session: requests.Session, timeout: float) -> dict[str, Any] | None:
    try:
        resp = session.get(
            f"http://ip-api.com/json/{ip}?fields=status,proxy,hosting", timeout=timeout, headers=UA
        )
        payload = resp.json()
        if payload.get("status") != "success":
            return None
        return {"proxy": bool(payload.get("proxy")), "hosting": bool(payload.get("hosting"))}
    except Exception:
        return None


def score_ip(ip: str, session: requests.Session, timeout: float) -> dict[str, Any] | None:
    """查询单个 IP 的多来源数据并合成纯净度 (0-100)."""
    scam = _scamalytics(ip, session, timeout)
    proxycheck = _proxycheck(ip, session, timeout)
    ipapi = _ipapi(ip, session, timeout)

    risks = [value for value in (scam, (proxycheck or {}).get("risk")) if value is not None]
    if not risks and not ipapi and not proxycheck:
        return None

    base = sum(risks) / len(risks) if risks else 0.0
    penalty = 0
    if proxycheck and proxycheck.get("proxy"):
        penalty += 15
    if ipapi and ipapi.get("proxy"):
        penalty += 10
    if ipapi and ipapi.get("hosting"):
        penalty += 20
    purity = max(0, min(100, round(100 - base - penalty)))
    return {
        "purity": purity,
        "scamalytics": scam,
        "proxycheck": proxycheck,
        "ipapi": ipapi,
    }


def annotate(
    results: list[dict[str, Any]], history: list[dict[str, Any]], cfg: Config
) -> int:
    """给结果节点附加纯净度; 历史 IP 复用旧分数, 仅对新 IP 查询."""
    if not cfg.purity_check:
        return 0

    cached: dict[str, dict[str, Any]] = {}
    for item in history:
        ip = (item.get("exit") or {}).get("ip")
        score = item.get("purity")
        if ip and isinstance(score, dict) and score.get("purity") is not None:
            cached[str(ip)] = score

    pending: list[str] = []
    for item in results:
        if not item.get("success"):
            continue
        ip = (item.get("exit") or {}).get("ip")
        if not ip:
            continue
        if ip in cached or ip in pending:
            continue
        pending.append(ip)
    pending = pending[: cfg.purity_max_new]

    fetched: dict[str, dict[str, Any]] = {}
    if pending:
        session = requests.Session()
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(score_ip, ip, session, cfg.purity_timeout): ip for ip in pending}
            for future in as_completed(futures):
                ip = futures[future]
                try:
                    score = future.result()
                except Exception:
                    score = None
                if score:
                    fetched[ip] = score
        session.close()

    merged = {**cached, **fetched}
    annotated = 0
    for item in results:
        ip = (item.get("exit") or {}).get("ip")
        if not ip:
            continue
        score = merged.get(ip)
        if score:
            item["purity"] = score
            annotated += 1
    log.info(
        "纯净度: 历史复用 %d, 本轮新查 %d/%d, 已标注 %d 个节点",
        len(cached),
        len(fetched),
        len(pending),
        annotated,
    )
    return annotated
