"""MQTTS 证书检查（§8.2：公网 8105 必须 TLS；证书由 nginx/certbot 管）。

    python scripts/cert_check.py --host mqtt.example.com --port 8105
"""

from __future__ import annotations

import argparse
import socket
import ssl


def check(host: str, port: int) -> dict:
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            cert = tls.getpeercert()
            return {"ok": True, "tls_version": tls.version(), "cipher": tls.cipher()[0],
                    "not_after": cert.get("notAfter"), "subject": dict(x[0] for x in cert["subject"])}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=8105)
    args = p.parse_args()
    try:
        print(check(args.host, args.port))
    except Exception as exc:  # noqa: BLE001
        print({"ok": False, "error": str(exc)})
