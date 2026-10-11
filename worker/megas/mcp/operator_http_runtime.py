from __future__ import annotations

import argparse
import ssl
import sys
from typing import Any

from .operator_http import (
    OperatorHTTPConfig,
    OperatorHTTPHandler,
    OperatorHTTPServer,
)
from .operator_runtime import MegasOperatorMCPServer


def create_operator_http_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    bearer_env: str | None = None,
    tls_cert: str | None = None,
    tls_key: str | None = None,
    allowed_origins: tuple[str, ...] | list[str] = (),
    operator: MegasOperatorMCPServer | None = None,
) -> OperatorHTTPServer:
    config = OperatorHTTPConfig(
        host=host,
        port=port,
        bearer_env=bearer_env,
        tls_cert=tls_cert,
        tls_key=tls_key,
        allowed_origins=tuple(allowed_origins),
    )
    config.validate()
    server = OperatorHTTPServer(
        (config.host, config.port),
        OperatorHTTPHandler,
        operator=operator or MegasOperatorMCPServer(transport="streamable-http"),
        config=config,
    )
    if config.tls_cert and config.tls_key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(config.tls_cert, config.tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def serve_operator_http(**kwargs: Any) -> None:
    server = create_operator_http_server(**kwargs)
    scheme = "https" if server.config.tls_cert else "http"
    host, port = server.server_address[:2]
    print(f"MegasMCP Streamable HTTP: {scheme}://{host}:{port}/mcp", file=sys.stderr, flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="megas serve-http",
        description="Serve the account-scoped MegasMCP operator over stateless Streamable HTTP.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--bearer-env", help="environment variable containing the HTTP bearer secret")
    parser.add_argument("--tls-cert", help="PEM TLS certificate path")
    parser.add_argument("--tls-key", help="PEM TLS private-key path")
    parser.add_argument("--allow-origin", action="append", default=[], help="explicit allowed browser origin; repeat as needed")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        serve_operator_http(
            host=args.host,
            port=args.port,
            bearer_env=args.bearer_env,
            tls_cert=args.tls_cert,
            tls_key=args.tls_key,
            allowed_origins=tuple(args.allow_origin),
        )
    except (ValueError, RuntimeError, PermissionError, OSError, ssl.SSLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


__all__ = ["build_parser", "create_operator_http_server", "main", "serve_operator_http"]
