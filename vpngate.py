#!/usr/bin/env python3
"""VPN Gate SSTP 节点检测流水线 (入口).

流程:
  1. 获取 VPN Gate 原始节点 (官方 API, 失败回退镜像)
  2. 只保留带 TCP 入口的 SSTP 节点并去重
  3. 并发调用检测 Worker
  4. 合并静态优选域名与优选 API 入口
  5. 生成 public/data.json + public/index.html + public/nodes.txt
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor

import checker
import config
import edge
import logsetup
import output
import purity
import sources
import state
from config import mask_secret


class FatalError(RuntimeError):
    """业务上不可继续的错误 (对应旧版 die)."""


def main() -> None:
    """执行一次完整流水线."""
    logsetup.setup()
    cfg = config.load_config()
    cfg.validate()

    gate_log = logsetup.get("VPN GATE")
    worker_log = logsetup.get("CLOUDFLARE WORKER")

    session = checker.make_session(cfg.concurrency)
    rows, source = sources.fetch_vpngate(cfg, session)
    raw_count = len(rows)
    if raw_count == 0:
        raise FatalError("VPN Gate 返回 0 个原始节点 (数据源异常, 不允许生成空结果)")

    sstp_nodes = sources.to_sstp_nodes(rows)
    sstp_count = len(sstp_nodes)
    if sstp_count == 0:
        raise FatalError(
            f"从 {raw_count} 个原始节点中没有解析出任何 SSTP(TCP) 节点 — 数据格式可能已变化, 需要人工适配"
        )
    previous = state.fetch_previous_state(cfg.previous_data_url, cfg.http_timeout)
    history = previous.nodes
    node_stats = previous.stats
    unique_nodes = sources.dedupe(list(sstp_nodes) + history)
    gate_log.info(
        "合并候选: 新 %d + 历史 %d -> 去重后 %d",
        len(sstp_nodes),
        len(history),
        len(unique_nodes),
    )
    external_blacklist = state.load_external_blacklist(cfg)
    unique_nodes, blacklist_skipped = state.filter_blacklisted(
        unique_nodes, node_stats, external_blacklist, cfg
    )
    if cfg.max_check_nodes > 0:
        unique_nodes = unique_nodes[: cfg.max_check_nodes]

    gate_log.info("获取原始节点: %d", raw_count)
    gate_log.info("SSTP 节点: %d", sstp_count)
    gate_log.info("去重后: %d", len(unique_nodes))

    worker_log.info(
        "提交检测: %d (并发 %d, 单请求超时 %ss)",
        len(unique_nodes),
        cfg.concurrency,
        cfg.check_timeout,
    )
    started = time.time()
    edge_executor = ThreadPoolExecutor(max_workers=1)
    edge_future = edge_executor.submit(edge.build_edge_pool, cfg)
    try:
        results = checker.check_all(unique_nodes, session, cfg)
        edge_hosts = edge_future.result()
    finally:
        edge_executor.shutdown(wait=True)
    elapsed = time.time() - started

    sampled = [item for item in results if len(item.get("latency_samples") or []) > 1][:10]
    for item in sampled:
        values = item["latency_samples"]
        worker_log.info(
            "采样对比 %s:%s -> %s ms (中位 %s, 极差 %s)",
            item["host"],
            item["port"],
            values,
            item["latency_ms"],
            max(values) - min(values),
        )

    checker.probe_latencies(results, cfg)
    rescued = checker.apply_probe_results(results)
    state.update_stats(node_stats, results, cfg)
    purity.annotate(results, history, cfg)
    compared = [item for item in results if item.get("worker_latency_ms") and item.get("probe_latency_ms")][:10]
    for item in compared:
        worker_log.info(
            "口径对比 %s:%s -> worker=%sms, probe=%sms",
            item["host"],
            item["port"],
            item["worker_latency_ms"],
            item["probe_latency_ms"],
        )
    success = [item for item in results if item.get("success")]
    failed = [item for item in results if not item.get("success")]
    worker_errors = [item for item in failed if item.get("worker_error")]

    worker_log.info("检测成功: %d", len(success))
    if rescued:
        worker_log.info("SSTP 兜底探测: 挽回 %d 个被 Worker 判失败的节点", rescued)
    worker_log.info(
        "检测失败: %d%s",
        len(failed),
        f" (其中 Worker 异常 {len(worker_errors)})" if worker_errors else "",
    )
    worker_log.info("耗时: %.1fs", elapsed)

    if not success:
        raise FatalError("本次没有任何可用节点 (全部检测失败) — 不生成空结果, 保留上次产物")

    data = output.build_outputs(
        results, raw_count, sstp_count, source, len(history), node_stats, blacklist_skipped
    )
    logsetup.get("RESULT").info("可用节点: %d", len(success))
    logsetup.get("RESULT").info("国家数量: %d", data["stats"]["countries"])

    data_path, html_path, nodes_path = output.write_outputs(cfg, data, edge_hosts)
    for path in (data_path, html_path, nodes_path):
        relative = mask_secret(os.path.relpath(path, cfg.repo_dir), cfg.secret_key)
        logsetup.get("WEBSITE").info("生成 %s", relative)
    logsetup.get("USAGE").info(
        "自动轮换: 把 %s 填入 edgetunnel 后台「自定义优选IP」框 (一次配置, 之后自动更新)",
        mask_secret(cfg.nodes_url, cfg.secret_key),
    )
    logsetup.get("WEBSITE").info("完成 (GitHub Pages 部署由 workflow 执行)")
    session.close()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except (
        FatalError,
        config.ConfigError,
        sources.SourceError,
        edge.EdgePoolError,
        output.OutputError,
    ) as exc:
        logsetup.die(str(exc))
    except Exception as exc:  # noqa: BLE001 - 顶层兜底, 与旧版行为一致
        logsetup.die(f"程序异常: {type(exc).__name__}: {exc}")
