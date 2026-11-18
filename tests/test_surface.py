"""Route-surface consistency — dead endpoints cannot quietly accumulate.

Born from the v5.1.1 cleanup: seven routes had zero callers (not in the
dashboard JS, not in the agent-skill file, not in the docs). These tests pin
the contract in BOTH directions:

1. Every route the agent exposes must have a real consumer: the dashboard
   JS, the agent-skill file, the README endpoint list, or the dashboard HTML.
   (Some routes are agent-only by design — /api/jobs, /api/boot — the
   agent-skill file counts as their consumer.)
2. Every endpoint the dashboard JS calls must exist on the agent — a stale
   JS call is a broken button, not just dead code.
"""
import re
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SERVER = BASE / "novadroid.py"
CONSUMERS = [BASE / "static" / "app.js",
             BASE / "core" / "agentskill.py",
             BASE / "README.md",
             BASE / "static" / "index.html"]


def server_routes() -> set[str]:
    src = SERVER.read_text(encoding="utf-8")
    return set(re.findall(r'"/api/[a-z0-9][a-z0-9/_-]*"', src))


def consumer_texts() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in CONSUMERS)


def js_endpoints() -> set[str]:
    src = (BASE / "static" / "app.js").read_text(encoding="utf-8")
    return set(re.findall(r'api/[a-z0-9][a-z0-9/_-]*', src))


class TestServerRoutesHaveConsumers(unittest.TestCase):
    def test_every_route_is_referenced_somewhere(self):
        joined = consumer_texts()
        orphaned = sorted(r for r in server_routes() if r.strip('"') not in joined)
        self.assertEqual(
            orphaned, [],
            f"agent routes with NO consumer in JS/agent-skill/README/HTML — "
            f"either wire them up or remove them: {orphaned}")

    def test_ticket_paths_still_exist(self):
        # /api/screen, /api/screen/stream, /api/record/download are the three
        # ticket-authenticated binary views; if any is renamed, the ticket
        # scope in novadroid.py must move with it.
        routes = server_routes()
        for p in ("/api/screen", "/api/screen/stream", "/api/record/download"):
            self.assertIn(f'"{p}"', routes)


class TestJsCallsExistOnServer(unittest.TestCase):
    def test_every_js_endpoint_exists(self):
        # server literals look like "/api/x", JS captures look like "api/x" —
        # normalize both by stripping quotes and the leading slash.
        routes = {r.strip('"').lstrip("/") for r in server_routes()}
        missing = sorted(ep for ep in js_endpoints()
                         if not any(r == ep
                                    or r.startswith(ep + "/")
                                    or r.startswith(ep + "?")
                                    for r in routes))
        self.assertEqual(
            missing, [],
            f"dashboard JS calls endpoints the agent does not expose "
            f"(stale frontend?): {missing}")


if __name__ == "__main__":
    unittest.main()
