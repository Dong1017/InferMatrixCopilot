"""Browser draft hashes must match the server's reviewed-content contract."""
import hashlib
import json
import re
from pathlib import Path
import shutil
import subprocess

import pytest


NODE = shutil.which("node")
MODULE = Path(__file__).resolve().parents[1] / "src/infermatrix_copilot/rfc_service/web/roadmap-chat.mjs"


@pytest.mark.skipif(not NODE, reason="Node is required for RFC chat browser contracts")
def test_browser_digest_matches_server_for_unicode_and_exact_raw_markdown():
    values = [
        ("RFC", "body"),
        ("中文方案 👋", "原文\n```mermaid\nflowchart LR\nF1 --> F2\n```\n"),
        ('Quote " and slash \\', "line\r\nnext"),
        ("", ""),
        ("Large RFC", "保留全部原文\n" * 10000),
    ]
    # These exported helpers do not depend on DOM/locale. Remove only the browser
    # browser-only imports so Node executes the actual shipped digest implementation.
    source = re.sub(r"^import .+;\n", "", MODULE.read_text(encoding="utf-8"), flags=re.M)
    program = source + "\nconst results=" + json.dumps(values) + ".map(([title,body])=>draftDigest(title,body));\nprocess.stdout.write(JSON.stringify(results));"
    result = subprocess.run([NODE, "--input-type=module"], input=program, text=True, encoding="utf-8", capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    expected = [hashlib.sha256(json.dumps({"title": title, "body": body}, ensure_ascii=False,
                                        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                for title, body in values]
    assert json.loads(result.stdout) == expected
