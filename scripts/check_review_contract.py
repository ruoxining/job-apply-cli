"""Exercise the pinned reviewer against a local synthetic provider; no real model calls."""

import importlib.util
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

spec = importlib.util.spec_from_file_location("ai_review", Path(__file__).with_name("ai_review.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
seen = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        seen.append(body)
        answer = "review:\n  key_issues_to_review: []\n  relevant_tests: Yes\n  security_concerns: No\n  estimated_effort_to_review_[1-5]: 1\n"
        data = json.dumps(
            {
                "id": "fixture",
                "object": "chat.completion",
                "created": 1,
                "model": body.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": answer},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


server = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["PR_AGENT_API_KEY"] = "synthetic-fixture-key"
os.environ["PR_AGENT_API_BASE"] = f"http://127.0.0.1:{server.server_port}/v1"
os.environ["PR_AGENT_MODEL"] = "openai/glm-5.3-flash"
try:
    result = m.run_model(
        "diff --git a/src/test.py b/src/test.py\n--- a/src/test.py\n+++ b/src/test.py\n@@ -1 +1 @@\n-print(1)\n+print(2)\n"
    )
    assert result["key_issues_to_review"] == [], result
    assert len(seen) == 1, len(seen)
    assert any("Project invariants" in str(message) for message in seen[0]["messages"])
    print(
        "Pinned PR-Agent CLI, trusted policy, local fake provider and structured-output contract passed."
    )
finally:
    server.shutdown()
