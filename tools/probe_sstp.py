"""一次性诊断: 验证 OpenVPN 配置里的 remote 端口是否就是 SSTP 端口.

在 GitHub Actions runner 上运行 (本地网络到 opengw.net 不可达).
对每个抽样节点同时探测 remote 端口与 443, 打印 TLS 版本与 SSTP 应答首行.
"""

from __future__ import annotations

import os
import socket
import ssl
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

import config
import sources

SSTP_PATH = "/sra_{BA195980-CD49-458b-9E23-C84EE0ADCD75}/"
SAMPLE = 10


def probe(host: str, port: int, timeout: float = 12.0) -> tuple[str, bytes]:
    """建立 TLS 并发送 SSTP_DUPLEX_POST, 返回 (TLS 版本, 应答前 80 字节)."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.set_ciphers("DEFAULT:@SECLEVEL=0")
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            request = (
                f"SSTP_DUPLEX_POST {SSTP_PATH} HTTP/1.1\r\n"
                f"Host: {host}\r\n"
                "Content-Length: 18446744073709551615\r\n"
                "\r\n"
            )
            tls.sendall(request.encode())
            return tls.version() or "?", tls.recv(80)


def main() -> None:
    cfg = config.load_config()
    session = requests.Session()
    rows, source = sources.fetch_vpngate(cfg, session)
    nodes = sources.dedupe(sources.to_sstp_nodes(rows))[:SAMPLE]
    print(f"source={source} candidates={len(nodes)}")

    remote_ok = 0
    port443_ok = 0
    for node in nodes:
        host, port = node["host"], int(node["port"])
        for label, probe_port in (("remote", port), ("443", 443)):
            try:
                version, line = probe(host, probe_port)
                good = b"HTTP/" in line and b" 200" in line
                if good and label == "remote":
                    remote_ok += 1
                if good and label == "443":
                    port443_ok += 1
                print(f"{host}:{probe_port} [{label}] TLS={version} resp={line[:40]!r} {'SSTP_OK' if good else 'NOT_SSTP'}")
            except Exception as exc:
                print(f"{host}:{probe_port} [{label}] FAIL {type(exc).__name__}: {exc}")

    print(f"RESULT remote_port_ok={remote_ok}/{len(nodes)} port443_ok={port443_ok}/{len(nodes)}")
    if remote_ok == 0:
        raise SystemExit("remote 端口没有任何 SSTP 应答 — 端口假设不成立")


if __name__ == "__main__":
    main()
