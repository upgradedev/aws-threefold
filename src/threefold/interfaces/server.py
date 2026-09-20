"""Standalone HTTP development server for Threefold.

Runs standard library http.server on port 8001 (or custom port),
routing incoming HTTP requests directly through the AWS Lambda handler.
Zero third-party dependencies required.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Ensure src directory is on sys.path for direct script execution
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from threefold.interfaces.api_handlers import lambda_handler

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("threefold.server")


class ThreefoldHTTPRequestHandler(BaseHTTPRequestHandler):
    """Bridges HTTP requests to lambda_handler with full CORS support."""

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        self._handle_request("GET")

    def do_POST(self) -> None:
        self._handle_request("POST")

    def _handle_request(self, method: str) -> None:
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        query_params = {k: v[0] if len(v) == 1 else v for k, v in parse_qs(parsed_url.query).items()}

        body_bytes = b""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 0:
            body_bytes = self.rfile.read(content_length)

        # Reconstruct standard AWS API Gateway Lambda event
        event = {
            "httpMethod": method,
            "path": path,
            "rawPath": path,
            "queryStringParameters": query_params if query_params else None,
            "headers": dict(self.headers),
            "body": body_bytes.decode("utf-8") if body_bytes else None,
            "requestContext": {
                "http": {
                    "method": method,
                    "path": path,
                }
            },
        }

        try:
            response = lambda_handler(event, None)
        except Exception as exc:
            logger.exception("Internal error in handler: %s", exc)
            response = {
                "statusCode": 500,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": "Internal Server Error", "details": str(exc)}),
            }

        status_code = response.get("statusCode", 200)
        self.send_response(status_code)

        headers = response.get("headers", {})
        for h_name, h_val in headers.items():
            self.send_header(h_name, h_val)
        self._send_cors_headers()
        self.end_headers()

        resp_body = response.get("body", "")
        if isinstance(resp_body, str):
            self.wfile.write(resp_body.encode("utf-8"))
        elif isinstance(resp_body, (bytes, bytearray)):
            self.wfile.write(resp_body)

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("%s - - [%s] %s", self.address_string(), self.log_date_time_string(), format % args)


def run_server(port: int = 8001, host: str = "0.0.0.0") -> None:
    server_address = (host, port)
    # Threading, not the single-connection server: a browser holds this open with
    # keep-alive, and every curl issued against the same port while a page is open
    # would otherwise block until that tab is closed.
    httpd = ThreadingHTTPServer(server_address, ThreefoldHTTPRequestHandler)
    logger.info("Threefold live backend listening on http://%s:%d", host, port)
    logger.info("Ready to intercept agent tool calls and serve live REST API requests")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server shutting down...")
        httpd.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Threefold Local REST Server")
    parser.add_argument("--port", type=int, default=8001, help="Port to listen on (default: 8001)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host interface (default: 0.0.0.0)")
    args = parser.parse_args()
    run_server(port=args.port, host=args.host)
