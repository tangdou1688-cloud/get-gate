"""历史节点复用: 从上一轮已发布的 data.json 取回候选节点.

设计目的: 刷新时不要用"本轮新抓到的节点"整体替换线上列表. 上一轮可用、本轮仍可用
的节点应当继续保留. 这里把上一轮 data.json 的 available 列表作为历史候选, 与本轮
新节点合并后统一复检, 通过即保留.
"""

from __future__ import annotations

from typing import Any

import requests

from logsetup import get

log = get("VPN GATE")


def fetch_previous_nodes(url: str, timeout: float) -> list[dict[str, Any]]:
    """读取上一轮 data.json 的 available 列表, 返回可复检的节点候选."""
    if not url:
        return []
    try:
        resp = requests.get(
            url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 (gate-checker)"}
        )
        if resp.status_code != 200:
            log.info("历史节点: 上一轮 data.json 返回 HTTP %s, 跳过", resp.status_code)
            return []
        data = resp.json()
    except Exception as exc:
        log.info("历史节点: 读取上一轮 data.json 失败 (%s: %s), 跳过", type(exc).__name__, exc)
        return []

    available = data.get("available") if isinstance(data, dict) else None
    if not isinstance(available, list):
        return []

    nodes: list[dict[str, Any]] = []
    for item in available:
        if not isinstance(item, dict):
            continue
        host = str(item.get("host") or "").strip().lower()
        try:
            port = int(item.get("port"))
        except (TypeError, ValueError):
            continue
        if not host or not (1 <= port <= 65535):
            continue
        nodes.append(
            {
                "host": host,
                "port": port,
                "ip": str(item.get("ip") or ""),
                "country": str(item.get("country") or ""),
                "country_code": str(item.get("country_code") or ""),
            }
        )
    log.info("历史节点: 从上一轮 data.json 取回 %d 个候选", len(nodes))
    return nodes
