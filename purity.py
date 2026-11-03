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
            annotated += 1
    log.info(
        "纯净度(Scamalytics): 历史复用 %d, 本轮新查 %d/%d, 已标注 %d 个节点",
        len(cached),
        len(fetched),
        len(pending),
        annotated,
    )
    return annotated
