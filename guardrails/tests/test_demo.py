"""Runs the full end-to-end demo (fake Frappe + real API server) as a test."""
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_demo_end_to_end():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "demo.py"), "--start", "--no-color",
         "--api-port", str(free_port()), "--frappe-port", str(free_port())],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    assert "All guardrails behaved as expected." in proc.stdout
