#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from typing import Dict, List, Optional, Tuple


CHECK_COUNT = 0
MENU_SCREEN_PATH = "/v1/machine:menu_screen"
MENU_BUTTON_PATH = "/v1/machine:menu_button"
MENU_SCREEN_UNAVAILABLE = "Menu screen unavailable."
FORBIDDEN = "Forbidden."


class Failure(RuntimeError):
    pass


@contextmanager
def check(label: str):
    global CHECK_COUNT
    CHECK_COUNT += 1
    print(f"[{CHECK_COUNT:02d}] {label} ... ", end="", flush=True)
    try:
        yield
    except Exception:
        print("FAIL", flush=True)
        raise
    print("OK", flush=True)


def format_exception(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.URLError) and getattr(exc, "reason", None) is not None:
        return f"{exc} ({exc.reason})"
    return str(exc)


class RestSession:
    def __init__(self, host: str, password: Optional[str], timeout: float) -> None:
        self.host = host
        self.password = password
        self.timeout = timeout

    def url(self, path: str, params: Optional[Dict[str, object]] = None) -> str:
        query = ""
        if params:
            query = "?" + urllib.parse.urlencode(params)
        return f"http://{self.host}{path}{query}"

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, object]] = None,
        payload: Optional[Dict[str, object]] = None,
        use_password: bool = True,
    ) -> Tuple[int, Dict[str, str], bytes]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers: Dict[str, str] = {}
        if self.password and use_password:
            headers["X-Password"] = self.password
        if body is not None:
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(self.url(path, params), data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, dict(response.headers.items()), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers.items()), exc.read()
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            raise Failure(f"{method} {self.url(path, params)} failed: {format_exception(exc)}") from exc


def header_value(headers: Dict[str, str], name: str) -> str:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return ""


def parse_json(label: str, body: bytes) -> Dict[str, object]:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise Failure(f"{label}: response body is not valid JSON: {body[:160]!r}") from exc
    if not isinstance(data, dict):
        raise Failure(f"{label}: expected JSON object, got {data!r}")
    return data


def require_error(label: str, body: bytes, message: str) -> None:
    data = parse_json(label, body)
    errors = data.get("errors")
    if not isinstance(errors, list) or message not in errors:
        raise Failure(f"{label}: expected errors to contain {message!r}, got {data!r}")


def verify_binary_contract(status: int, headers: Dict[str, str], body: bytes) -> None:
    if status != 200:
        raise Failure(f"expected HTTP 200, got HTTP {status}: {body[:160]!r}")

    content_type = header_value(headers, "Content-Type")
    if "application/octet-stream" not in content_type:
        raise Failure(f"expected application/octet-stream, got {content_type!r}")
    if len(body) != 2000:
        raise Failure(f"expected 2000 response bytes, got {len(body)}")

    chars = body[:1000]
    attrs = body[1000:]
    if len(chars) != 1000 or len(attrs) != 1000:
        raise Failure("response did not split into two 1000-byte planes")
    if len(set(chars)) == 1 and len(set(attrs)) == 1:
        raise Failure("snapshot is trivially uniform in both planes")


def run_contract(session: RestSession, open_menu: bool) -> None:
    with check("GET menu_screen contract"):
        status, headers, body = session.request("GET", MENU_SCREEN_PATH)
        if status == 404 and not open_menu:
            require_error("menu_screen unavailable", body, MENU_SCREEN_UNAVAILABLE)
            print("menu_screen_test: menu unavailable; binary contract not exercised without --open-menu", file=sys.stderr)
            return
        verify_binary_contract(status, headers, body)


def run_unavailable(session: RestSession, open_menu: bool) -> None:
    if open_menu:
        print("menu_screen_test: SKIP unavailable (--open-menu expects an active menu)", file=sys.stderr)
        return
    with check("GET menu_screen unavailable"):
        status, headers, body = session.request("GET", MENU_SCREEN_PATH)
        if status != 404:
            raise Failure(f"expected HTTP 404 unavailable state, got HTTP {status}")
        content_type = header_value(headers, "Content-Type")
        if "application/json" not in content_type:
            raise Failure(f"expected application/json, got {content_type!r}")
        require_error("menu_screen unavailable", body, MENU_SCREEN_UNAVAILABLE)


def run_auth(session: RestSession) -> None:
    if not session.password:
        print("menu_screen_test: SKIP auth (--password not supplied)", file=sys.stderr)
        return
    with check("GET menu_screen auth"):
        status, headers, body = session.request("GET", MENU_SCREEN_PATH, use_password=False)
        if status != 403:
            raise Failure(f"expected HTTP 403 without X-Password, got HTTP {status}")
        content_type = header_value(headers, "Content-Type")
        if "application/json" not in content_type:
            raise Failure(f"expected application/json, got {content_type!r}")
        require_error("menu_screen auth", body, FORBIDDEN)


def expand_tests(selected: Optional[List[str]]) -> List[str]:
    if not selected:
        selected = ["contract", "auth"]
    expanded: List[str] = []
    for name in selected:
        names = ["contract", "unavailable", "auth"] if name == "all" else [name]
        for item in names:
            if item not in expanded:
                expanded.append(item)
    return expanded


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate /v1/machine:menu_screen on real firmware.")
    parser.add_argument("-H", "--host", default=os.environ.get("U64_INPUT_HOST", "u64"))
    parser.add_argument("-r", "--rest-host", default=os.environ.get("U64_INPUT_REST_HOST"))
    parser.add_argument(
        "-p",
        "--password",
        default=os.environ.get("U64_INPUT_PASSWORD", os.environ.get("C64U_PASSWORD")),
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=float,
        default=float(os.environ.get("U64_INPUT_TIMEOUT", "5.0")),
    )
    parser.add_argument(
        "--test",
        action="append",
        choices=("all", "contract", "unavailable", "auth"),
    )
    parser.add_argument(
        "--open-menu",
        action="store_true",
        help="Toggle the menu open for the positive menu_screen contract check.",
    )
    args = parser.parse_args()

    rest_host = args.rest_host or args.host
    session = RestSession(rest_host, args.password, args.timeout)
    tests = expand_tests(args.test)
    opened = False

    try:
        if args.open_menu and "contract" in tests:
            with check("open menu"):
                status, _, body = session.request("PUT", MENU_BUTTON_PATH)
                if status != 200:
                    raise Failure(f"menu_button open failed with HTTP {status}: {body[:160]!r}")
            opened = True
            time.sleep(0.25)

        if "contract" in tests:
            run_contract(session, args.open_menu)
        if "unavailable" in tests:
            run_unavailable(session, args.open_menu)
        if "auth" in tests:
            run_auth(session)
    finally:
        if opened:
            with check("close menu"):
                status, _, body = session.request("PUT", MENU_BUTTON_PATH)
                if status != 200:
                    raise Failure(f"menu_button close failed with HTTP {status}: {body[:160]!r}")

    print(f"menu_screen_test: OK ({CHECK_COUNT} checks)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Failure as exc:
        print(f"menu_screen_test: FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
