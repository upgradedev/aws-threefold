"""The development server has to answer what the deployed function answers.

It is the same handler behind both, but the standard library decides which
methods exist before the handler is reached: without do_HEAD it answered 501
where the deployment answers 200, so a probe pointed at a local run reported a
service that was not there.

The server is bound to a loopback port the operating system chooses, and shut
down in the same test. Nothing leaves the machine.
"""
from __future__ import annotations

import http.client
import threading
from http.server import ThreadingHTTPServer

import pytest

from threefold.interfaces.server import ThreefoldHTTPRequestHandler


@pytest.fixture
def server() -> tuple[str, int]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ThreefoldHTTPRequestHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[0], httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _request(address: tuple[str, int], method: str, path: str):
    connection = http.client.HTTPConnection(address[0], address[1], timeout=5)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        return response.status, response.getheader("Content-Type"), response.read()
    finally:
        connection.close()


def test_head_answers_the_status_a_get_would_with_no_body(server) -> None:
    get_status, get_type, get_body = _request(server, "GET", "/status")
    head_status, head_type, head_body = _request(server, "HEAD", "/status")

    assert (head_status, head_type) == (get_status, get_type) == (200, "application/json")
    assert get_body, "The control: a GET does return a body"
    assert head_body == b""


def test_head_on_an_unknown_path_is_a_404_rather_than_a_501(server) -> None:
    status, _, body = _request(server, "HEAD", "/no-such-page.html")
    assert status == 404
    assert body == b""
