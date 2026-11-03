"""VPN Gate 数据源: 抓取、解析、筛选 SSTP 节点、去重."""

from __future__ import annotations

import base64
import csv
import io
import re
from typing import Any, Iterable

import requests

from config import Config
from logsetup import get

log = get("VPN GATE")

_PROTO_TCP_RE = re.compile(r"^proto\s+(tcp|tcp4|tcp6)\b", re.M)
_REMOTE_RE = re.compile(r"^remote\s+(\S+)\s+(\d+)", re.M)
_HOST_RE = re.compile(r"^[a-z0-9.-]+\.opengw\.net$")
_COUNTRY_CODE_RE = re.compile(r"^[A-Z]{2}$")
_UNSAFE_TEXT_RE = re.compile(r"[#$\r\n\x00-\x1f\x7f]")


def _sanitize_text(value: object, limit: int = 64) -> str:
    """清洗第三方文本字段, 防止换行/#/$ 注入订阅行."""
    return _UNSAFE_TEXT_RE.sub(" ", str(value or "")).strip()[:limit]


class SourceError(RuntimeError):
    """数据源完全不可用."""


def fetch_vpngate(cfg: Config, session: requests.Session) -> tuple[list[dict[str, Any]], str]:
    """优先官方 API, 失败回退 GitHub 镜像; 返回 (rows, source)."""
    try:
        log.info("获取官方 API: %s", cfg.vpngate_api)
        resp = session.get(
            cfg.vpngate_api,
            timeout=cfg.http_timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; gate-checker)"},
        )
        resp.raise_for_status()
        rows = parse_csv(resp.text)
        if rows:
            log.info("主源(官方 API) 获取到 %d 个原始节点", len(rows))
            return rows, "vpngate.net/api/iphone"
        raise RuntimeError("官方 API 返回 0 行数据")
    except Exception as exc:
        log.info("官方 API 获取失败: %s", exc)

    try:
        log.info("回退镜像: %s", cfg.vpngate_mirror)
        resp = session.get(
            cfg.vpngate_mirror, timeout=cfg.http_timeout, headers={"User-Agent": "Mozilla/5.0"}
        )
        resp.raise_for_status()
        rows = parse_mirror_json(resp.json())
        if rows:
            log.info("回退源(镜像) 获取到 %d 个原始节点", len(rows))
            return rows, "github-mirror"
    except Exception as exc:
        log.info("回退镜像也失败: %s", exc)

    raise SourceError("VPN Gate 官方 API 与回退镜像均不可用, 数据源完全失败")


def parse_csv(text: str) -> list[dict[str, Any]]:
    """解析官方 API 的 CSV 文本 (表头以 # 开头)."""
    lines = [line for line in text.splitlines() if line.strip()]
    header_idx = None
    for index, line in enumerate(lines):
        if line.lstrip("#").startswith("HostName"):
            header_idx = index
            break
    if header_idx is None:
        raise RuntimeError("找不到 CSV 表头行 (HostName)")

    header = lines[header_idx].lstrip("#").split(",")
    data_lines = lines[header_idx + 1:]
    idx: dict[str, int] = {}
    for column in ("hostname", "ip", "countrylong", "countryshort", "openvpn_configdata_base64"):
        for index, name in enumerate(header):
            if name.strip().lstrip("*").lower() == column:
                idx[column] = index
                break
    if "openvpn_configdata_base64" not in idx:
        for index, name in enumerate(header):
            if "base64" in name.lower():
                idx["openvpn_configdata_base64"] = index
                break
    pos = {
        "hostname": idx.get("hostname", 0),
        "ip": idx.get("ip", 1),
        "countrylong": idx.get("countrylong", 5),
        "countryshort": idx.get("countryshort", 6),
        "openvpn_configdata_base64": idx.get("openvpn_configdata_base64", len(header) - 1),
    }

    rows: list[dict[str, Any]] = []
    for line in data_lines:
        fields = next(csv.reader(io.StringIO(line)))
        if len(fields) < 7:
            continue
        host = fields[pos["hostname"]].strip()
        ip = fields[pos["ip"]].strip()
        if not host or not ip:
            continue
        rows.append(
            {
                "host": host,
                "ip": ip,
                "country_long": fields[pos["countrylong"]].strip(),
                "country_short": fields[pos["countryshort"]].strip(),
                "config_b64": fields[pos["openvpn_configdata_base64"]].strip(),
            }
        )
    return rows


def parse_mirror_json(data: Any) -> list[dict[str, Any]]:
    """解析 GitHub 镜像的 JSON 结构 (servers 数组或单对象)."""
    servers: list[dict[str, Any]] = []
    items = data if isinstance(data, list) else [data]
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("servers"), list):
            servers.extend(item["servers"])
        elif isinstance(item, dict):
            servers.append(item)

    rows: list[dict[str, Any]] = []
    for server in servers:
        host = str(server.get("hostname") or server.get("host") or "").strip()
        ip = str(server.get("ip") or "").strip()
        if not host or not ip:
            continue
        rows.append(
            {
                "host": host,
                "ip": ip,
                "country_long": str(
                    server.get("countrylong") or server.get("country_long") or server.get("country") or ""
                ).strip(),
                "country_short": str(server.get("countryshort") or server.get("country_short") or "").strip(),
                "config_b64": str(
                    server.get("openvpn_configdata_base64") or server.get("config_b64") or ""
                ).strip(),
            }
        )
    return rows


def to_sstp_nodes(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """从 OpenVPN 配置中筛出带 TCP 入口的节点.

    重要假设 (保持旧行为, 未改动):
        这里用 OpenVPN 配置里的 `proto tcp` 判断"可走 TCP 入口", 并把 OpenVPN
        `remote` 的端口直接当作 SSTP 端口使用. 真实 SSTP 服务通常监听 443,
        两者可能不一致 —— 若上游数据格式变化, 需要人工核对/适配.
    """
    nodes: list[dict[str, Any]] = []
    for row in rows:
        config = ""
        if row["config_b64"]:
            try:
                config = base64.b64decode(row["config_b64"], validate=False).decode("utf-8", "replace")
            except Exception:
                config = ""
        if not _PROTO_TCP_RE.search(config):
            continue
        ports: list[int] = []
        for matched in _REMOTE_RE.finditer(config):
            port = int(matched.group(2))
            if 1 <= port <= 65535 and port not in ports:
                ports.append(port)
        if not ports:
            continue
        host = str(row["host"]).strip().lower()
        if not host.endswith(".opengw.net"):
            host = f"{host}.opengw.net"
        if not _HOST_RE.fullmatch(host):
            continue
        country_code = str(row["country_short"]).strip().upper()
        if not _COUNTRY_CODE_RE.fullmatch(country_code):
            continue
        country = _sanitize_text(row["country_long"]) or country_code
        for port in ports:
            nodes.append(
                {
                    "host": host,
                    "port": port,
                    "ip": row["ip"],
                    "country": country,
                    "country_code": country_code,
                }
            )
    return nodes


def dedupe(nodes: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 (host, port) 去重, 保持原始顺序."""
    seen: set[tuple[str, int]] = set()
    unique: list[dict[str, Any]] = []
    for node in nodes:
        key = (str(node["host"]).lower(), int(node["port"]))
        if key in seen:
            continue
        seen.add(key)
        unique.append(node)
    return unique
