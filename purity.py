"""IP 纯净度: 仅使用 Scamalytics 欺诈分 (0-100, 越低越干净).

purity = 100 - fraud_score, 结果夹在 0-100.
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


def score_ip(ip: str, session: requests.Session, timeout: float) -> dict[str, Any] | None:
    """查询 Scamalytics 欺诈分, 换算为 0-100 纯净度."""
    try:
        resp = session.get(f"https://scamalytics.com/ip/{ip}", timeout=timeout, headers=UA)
        if resp.status_code != 200:
            return None
        matched = _FRAUD_RE.search(resp.text)
        if not matched:
            return None
        fraud = int(matched.group(1))
    except Exception:
        return None

    purity = max(0, min(100, 100 - fraud))
    return {"purity": purity, "scamalytics": fraud}


def _latency_score(latency_ms: Any) -> float:
    """延迟换算 0-10: <=800ms 满分, 3000ms 为 5 分, >=6000ms 为 0."""
    if not isinstance(latency_ms, (int, float)):
        return 5.0
    ms = float(latency_ms)
    if ms <= 800:
        return 10.0
    if ms <= 3000:
        return 10.0 - (ms - 800) / 2200 * 5.0
    if ms <= 6000:
        return 5.0 - (ms - 3000) / 3000 * 5.0
    return 0.0


def quality_score(node: dict[str, Any]) -> int | None:
    """综合质量分 0-10 (权重规则见 README).

    风险 45% + 类型 20% + 延迟 10% + 验证方式 10% + 解锁潜力 15%.
    解锁能力无法在 CI 中实测, 这里用"住宅类型"作为潜力代理值.
    """
    fraud = (node.get("purity") or {}).get("scamalytics")
    if not isinstance(fraud, (int, float)):
        return None
    risk = (100.0 - float(fraud)) / 10.0
    kind = node.get("residential") or "unknown"
    type_score = {"residential": 10.0, "datacenter": 4.0, "unknown": 6.0}.get(kind, 6.0)
    unlock_score = {"residential": 10.0, "datacenter": 1.0, "unknown": 5.0}.get(kind, 5.0)
    verified = 10.0 if node.get("verified_by") == "worker" else 6.0
    total = (
        0.45 * risk
        + 0.20 * type_score
        + 0.10 * _latency_score(node.get("latency_ms"))
        + 0.10 * verified
        + 0.15 * unlock_score
    )
    return max(0, min(10, int(round(total))))


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
            if "proxycheck" in score or "ipapi" in score:
                continue  # 旧版多来源口径, 作废重查
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
        with ThreadPoolExecutor(max_workers=6) as pool:
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
            item["quality"] = quality_score(item)
            annotated += 1
    log.info(
        "纯净度(Scamalytics): 历史复用 %d, 本轮新查 %d/%d, 已标注 %d 个节点",
        len(cached),
        len(fetched),
        len(pending),
        annotated,
    )
    return annotated
