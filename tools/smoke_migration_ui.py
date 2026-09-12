"""Read-only Chromium smoke on a passed private migration rehearsal. No ratings."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rehearse_migration import run_worker, save
from ankiweb.anki_core.migration import compare
from ankiweb.app import create_app
from ankiweb.config import Settings


def main():
    import uvicorn
    from playwright.sync_api import sync_playwright, expect

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rehearsal", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    rehearsal = args.rehearsal.resolve()
    if not json.loads((rehearsal / "report.json").read_text())["passed"]:
        raise ValueError("requires a passed rehearsal")
    out = args.out.resolve()
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    collection = rehearsal / "target/collection.anki2"
    before = run_worker("snapshot", collection, out / "before.json")
    errors, failures, cards, blocked = [], [], [], []
    # Bind loopback only. Keep the socket reserved to avoid racing another server.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        settings = Settings(collection_path=collection, port=port)
        server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1",
                                               port=port, log_level="warning", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 15
            while not server.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError("private smoke server did not start")
                time.sleep(0.05)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    for width, height in ((1440, 1000), (390, 844)):
                        context = browser.new_context(viewport={"width": width, "height": height})
                        page = context.new_page()
                        page.on("pageerror", lambda error: errors.append(type(error).__name__))
                        page.on("response", lambda response: failures.append(response.status)
                                if "/api/" in response.url and response.status >= 400 else None)

                        def read_only(route):
                            request = route.request
                            if request.method == "GET" or request.url.endswith("/check"):
                                route.continue_()
                            else:
                                blocked.append(request.method)
                                route.abort()

                        page.route("**/api/**", read_only)
                        for language, title in (("spanish", "Испанский"), ("english", "Английский")):
                            page.goto(f"http://127.0.0.1:{port}/")
                            expect(page.get_by_label("Anki Core подключён")).to_be_visible()
                            panel = page.locator(".language-panel").filter(has=page.get_by_role("heading", name=title))
                            with page.expect_response(lambda response: "/api/review/next" in response.url) as issued:
                                panel.get_by_role("button", name="Продолжить").click()
                            payload = issued.value.json()
                            expect(page.locator(".question")).to_be_visible()
                            expect(page.locator(".error-notice")).to_have_count(0)
                            page.locator(".check-button").click()
                            expect(page.locator(".rating--good")).to_be_enabled()
                            expect(page.locator(".error-notice")).to_have_count(0)
                            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                            cards.append({"language": language, "width": width,
                                          "card_id": payload["card_id"], "kind": payload["question"]["kind"]})
                        context.close()
                finally:
                    browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            if thread.is_alive():
                raise RuntimeError("server did not close; do not open this collection again")
    after = run_worker("snapshot", collection, out / "after.json")
    unchanged = compare(before, after)
    report = {"passed": unchanged["equal"] and not errors and not failures and not blocked,
              "collection_unchanged": unchanged, "cases": cards, "page_errors": errors,
              "api_failures": failures, "blocked_mutations": blocked, "ratings_submitted": 0}
    save(out / "report.json", report)
    print(json.dumps(report, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
