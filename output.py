"""产物生成: data.json / index.html / nodes.txt."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Iterable

from config import Config
from logsetup import get

log = get("WEBSITE")


class OutputError(RuntimeError):
    """产物生成失败."""

COUNTRY_ZH = {
    "JP": "日本", "KR": "韩国", "US": "美国", "CA": "加拿大", "RU": "俄罗斯",
    "RO": "罗马尼亚", "TH": "泰国", "VN": "越南", "DE": "德国", "FR": "法国",
    "GB": "英国", "UK": "英国", "SG": "新加坡", "TW": "台湾", "HK": "香港",
    "CN": "中国", "AU": "澳大利亚", "NL": "荷兰", "SE": "瑞典", "CH": "瑞士",
    "IT": "意大利", "ES": "西班牙", "PL": "波兰", "IN": "印度", "BR": "巴西",
    "MX": "墨西哥", "ID": "印度尼西亚", "MY": "马来西亚", "PH": "菲律宾",
    "TR": "土耳其", "UA": "乌克兰", "CZ": "捷克", "GR": "希腊", "PT": "葡萄牙",
    "FI": "芬兰", "NO": "挪威", "DK": "丹麦", "IE": "爱尔兰", "BE": "比利时",
    "AT": "奥地利", "HU": "匈牙利", "AR": "阿根廷", "CL": "智利", "CO": "哥伦比亚",
    "NZ": "新西兰", "ZA": "南非", "IL": "以色列", "AE": "阿联酋", "SA": "沙特",
    "EG": "埃及", "HR": "克罗地亚", "BY": "白俄罗斯", "GD": "格林纳达",
    "LV": "拉脱维亚", "EE": "爱沙尼亚", "LT": "立陶宛", "SK": "斯洛伐克",
    "SI": "斯洛文尼亚", "BG": "保加利亚", "RS": "塞尔维亚", "GE": "格鲁吉亚",
    "MD": "摩尔多瓦", "AM": "亚美尼亚", "KZ": "哈萨克斯坦", "UZ": "乌兹别克斯坦",
    "MN": "蒙古", "NP": "尼泊尔", "LK": "斯里兰卡", "MM": "缅甸",
}

# 节点分类: (内部值, 名称后缀), 顺序决定 nodes.txt 中的排列
NETWORK_BUCKETS = (
    ("residential", "住宅"),
    ("datacenter", "机房"),
    ("unknown", "未知"),
)


def _node_sort_key(node: dict[str, Any]) -> tuple[Any, ...]:
    return (
        node.get("latency_ms") is None,
        node.get("latency_ms") or 0,
        node.get("host") or "",
    )


def build_outputs(
    results: Iterable[dict[str, Any]], raw_count: int, sstp_count: int, source: str
) -> dict[str, Any]:
    """汇总检测结果, 生成 data.json 内容."""
    results = list(results)
    available = [item for item in results if item.get("success")]
    countries: dict[str, dict[str, Any]] = {}
    for node in available:
        name = node["country"] or "未知"
        countries.setdefault(name, {"code": node["country_code"] or "?", "nodes": []})["nodes"].append(node)

    stats = {
        "raw_nodes": raw_count,
        "sstp_nodes": sstp_count,
        "checked": len(results),
        "success": len(available),
        "failed": len(results) - len(available),
        "countries": len(countries),
        "residential_est": sum(1 for node in available if node.get("residential") == "residential"),
        "datacenter_est": sum(1 for node in available if node.get("residential") == "datacenter"),
        "unknown_est": sum(1 for node in available if node.get("residential") == "unknown"),
    }

    by_country: dict[str, dict[str, Any]] = {}
    for name, group in countries.items():
        group["count"] = len(group["nodes"])
        group["residential"] = sum(1 for node in group["nodes"] if node.get("residential") == "residential")
        group["datacenter"] = sum(1 for node in group["nodes"] if node.get("residential") == "datacenter")
        group["unknown"] = sum(1 for node in group["nodes"] if node.get("residential") == "unknown")
        group["nodes"].sort(key=_node_sort_key)
        by_country[name] = group

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "source": source,
        "stats": stats,
        "countries": by_country,
        "available": available,
    }


def _pick_entry(edge_hosts: list[str], ports: list[int], index: int) -> str:
    """按序号轮转入口与端口 (端口轮转让 443/2053/8443 均匀分布)."""
    host = edge_hosts[index % len(edge_hosts)]
    if not ports:
        return host
    port = ports[index % len(ports)]
    base, sep, _ = host.rpartition(":")
    if not sep:
        return f"{host}:{port}"
    return f"{base}:{port}"


def build_nodes_text(
    data: dict[str, Any], edge_hosts: list[str], ports: list[int] | None = None
) -> str:
    """生成纯节点行: 每行 = 入口地址#国家-类别-序号-延迟$sstp://vpn:vpn@host:port."""
    if not edge_hosts:
        raise OutputError("优选入口池为空, 无法生成 nodes.txt")
    ports = ports or [443]

    lines: list[str] = []
    index = 0
    ordered = sorted(
        data["countries"].items(),
        key=lambda item: (-int(item[1].get("count") or 0), str(item[1].get("code") or item[0])),
    )
    for country_name, group in ordered:
        code = str(group.get("code") or "?").upper()
        zh = COUNTRY_ZH.get(code) or (code if code and code != "?" else country_name)
        nodes = sorted(group["nodes"], key=_node_sort_key)
        for kind, label in NETWORK_BUCKETS:
            bucket = [node for node in nodes if (node.get("residential") or "unknown") == kind]
            for order, node in enumerate(bucket, 1):
                entry = _pick_entry(edge_hosts, ports, index)
                index += 1
                latency = node.get("latency_ms")
                latency_suffix = f"-{int(latency)}ms" if latency is not None else ""
                lines.append(
                    f"{entry}#{zh}-{label}-{order:02d}{latency_suffix}"
                    f"$sstp://vpn:vpn@{node['host']}:{node['port']}"
                )
    return "\n".join(lines) + "\n"


def _render_html(template_path: str) -> str:
    if os.path.exists(template_path):
        with open(template_path, "r", encoding="utf-8") as handle:
            return handle.read()
    return (
        "<html><head><meta charset='utf-8'><title>VPN Gate SSTP 节点</title></head>"
        "<body><h1>VPN Gate SSTP 节点</h1><pre id='out'></pre></body>"
        "<script>fetch('data.json').then(r=>r.json()).then(d=>out.textContent=JSON.stringify(d.stats))"
        ".catch(e=>out.textContent='加载失败:'+e)</script></html>"
    )


def output_dir(cfg: Config) -> str:
    """产物目录: 配置了 SECRET_KEY 时发布到 public/<SECRET_KEY>/."""
    return os.path.join(cfg.public_dir, cfg.secret_key) if cfg.secret_key else cfg.public_dir


def write_outputs(cfg: Config, data: dict[str, Any], edge_hosts: list[str]) -> tuple[str, str, str]:
    """写出三个产物, 返回路径."""
    out_dir = output_dir(cfg)
    os.makedirs(out_dir, exist_ok=True)

    data_path = os.path.join(out_dir, "data.json")
    with open(data_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)

    html_path = os.path.join(out_dir, "index.html")
    with open(html_path, "w", encoding="utf-8") as handle:
        handle.write(_render_html(cfg.template_html))

    nodes_path = os.path.join(out_dir, "nodes.txt")
    with open(nodes_path, "w", encoding="utf-8") as handle:
        handle.write(build_nodes_text(data, edge_hosts, cfg.edge_ports))

    return data_path, html_path, nodes_path
