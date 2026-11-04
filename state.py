"""历史节点复用: 从上一轮已发布的 data.json 取回候选节点.

设计目的: 刷新时不要用"本轮新抓到的节点"整体替换线上列表. 上一轮可用、本轮仍可用
的节点应当继续保留. 这里把上一轮 data.json 的 available 列表作为历史候选, 与本轮
新节点合并后统一复检, 通过即保留.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from config import Config
from logsetup import get

log = get("VPN GATE")


@dataclass
class PreviousState:
    nodes: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, dict[str, Any]] = field(default_factory=dict)


def node_key(node: dict[str, Any]) -> str:
    return f"{str(node.get('host') or '').strip().lower()}:{int(node.get('port') or 0)}"


def normalize_key(raw: str) -> str | None:
    raw = raw.strip().lower()
    if not raw or raw.startswith("#"):
        return None
    host, sep, port = raw.rpartition(":")
    if not sep or not port.isdigit():
        host, port = raw, "443"
    if not host or not (1 <= int(port) <= 65535):
        return None
    if "." not in host:
        return None
    return f"{host}:{int(port)}"


def load_external_blacklist(cfg: Config) -> set[str]:
    """外部黑名单: 文件 + URL (每行 host:port 或 host)."""
    keys: set[str] = set()
    sources: list[str] = []
    if cfg.blacklist_file:
        sources.append(cfg.blacklist_file)
    sources.extend(url for url in re.split(r"[\s,]+", cfg.blacklist_url or "") if url)
    for source in sources:
        if source.startswith(("http://", "https://")):
            try:
                resp = requests.get(source, timeout=cfg.http_timeout, headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code != 200:
                    log.info("黑名单: %s 返回 HTTP %s, 跳过", source, resp.status_code)
                    continue
                lines = resp.text.splitlines()
            except Exception as exc:
                log.info("黑名单: 拉取 %s 失败 (%s: %s), 跳过", source, type(exc).__name__, exc)
                continue
        else:
            try:
                with open(source, "r", encoding="utf-8") as handle:
                    lines = handle.read().splitlines()
            except FileNotFoundError:
                continue
            except Exception as exc:
                log.info("黑名单: 读取 %s 失败 (%s: %s), 跳过", source, type(exc).__name__, exc)
                continue
        for line in lines:
            key = normalize_key(line)
            if key:
                keys.add(key)
    if keys:
        log.info("黑名单: 载入 %d 条外部记录", len(keys))
    return keys


def filter_blacklisted(
    nodes: list[dict[str, Any]], stats: dict[str, dict[str, Any]], external: set[str], cfg: Config
) -> tuple[list[dict[str, Any]], int]:
    """跳过外部黑名单与连续失败达到阈值的节点 (24 小时内不再重试)."""
    if not external and cfg.node_blacklist_fails <= 0:
        return nodes, 0
    now = time.time()
    kept: list[dict[str, Any]] = []
    skipped = 0
    for node in nodes:
        key = node_key(node)
        if key in external:
            skipped += 1
            continue
        if cfg.node_blacklist_fails > 0:
            item = stats.get(key) or {}
            streak = int(item.get("fail_streak") or 0)
            if streak >= cfg.node_blacklist_fails:
                last_fail = float(item.get("last_fail") or 0)
                if now - last_fail < cfg.blacklist_skip_hours * 3600:
                    skipped += 1
                    continue
                # 超过跳过窗口: 直接移出黑名单, 本轮不再检测, 下次重新出现时按新节点处理
                stats.pop(key, None)
                skipped += 1
                continue
        kept.append(node)
    if skipped:
        log.info(
            "黑名单: 跳过 %d 个连续失败/外部标记节点 (%sh 内不重试)",
            skipped,
            cfg.blacklist_skip_hours,
        )
    return kept, skipped


def update_stats(
    stats: dict[str, dict[str, Any]], results: list[dict[str, Any]], cfg: Config
) -> None:
    """更新每个节点的 seen/ok/fail_streak, 并按 STATS_TTL_DAYS 清理过期记录."""
    now = int(time.time())
    for item in results:
        key = node_key(item)
        record = stats.get(key) or {"seen": 0, "ok": 0, "fail_streak": 0}
        record["seen"] = int(record.get("seen") or 0) + 1
        if item.get("success"):
            record["ok"] = int(record.get("ok") or 0) + 1
            record["fail_streak"] = 0
            record["last_ok"] = now
        else:
            record["fail_streak"] = int(record.get("fail_streak") or 0) + 1
            record["last_fail"] = now
        record["last"] = now
        stats[key] = record
    cutoff = now - cfg.stats_ttl_days * 24 * 3600
    for key in [k for k, v in stats.items() if int(v.get("last") or 0) < cutoff]:
        stats.pop(key, None)


def fetch_previous_state(url: str, timeout: float) -> PreviousState:
    """读取上一轮 data.json 的 available 与 node_stats."""
    if not url:
        return PreviousState()
    try:
        resp = requests.get(
            url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 (gate-checker)"}
        )
        if resp.status_code != 200:
            log.info("历史节点: 上一轮 data.json 返回 HTTP %s, 跳过", resp.status_code)
            return PreviousState()
        data = resp.json()
    except Exception as exc:
        log.info("历史节点: 读取上一轮 data.json 失败 (%s: %s), 跳过", type(exc).__name__, exc)
        return PreviousState()

    available = data.get("available") if isinstance(data, dict) else None
    if not isinstance(available, list):
        return PreviousState()

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
                "exit": item.get("exit") if isinstance(item.get("exit"), dict) else None,
                "purity": item.get("purity") if isinstance(item.get("purity"), dict) else None,
            }
        )
    raw_stats = data.get("node_stats") if isinstance(data, dict) else None
    stats = {str(k): v for k, v in (raw_stats or {}).items() if isinstance(v, dict)}
    log.info("历史节点: 从上一轮 data.json 取回 %d 个候选, %d 条统计", len(nodes), len(stats))
    return PreviousState(nodes=nodes, stats=stats)
