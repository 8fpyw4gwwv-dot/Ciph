"""Launch the real server as a subprocess over stdio, like Claude Desktop does."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from fixture_data import PERSON, write_txt
from prokas_mcp.config import PROJECT_ROOT


def _config(tmp_path: Path) -> Path:
    cfg = {
        "export_dir": str(tmp_path / "Exporte"), "outbox_dir": str(tmp_path / "Outbox"),
        "jobs_dir": str(tmp_path / "Jobs"), "log_dir": str(tmp_path / "Logs"),
        "prokas_search_dirs": [str(tmp_path / "PROKAS")],
        "blocked_windows": [], "opening_hours": {"enabled": False},
        "ingest_wait_sec": 2, "ingest_poll_sec": 0.1,
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def _payload(res):
    if getattr(res, "structured_content", None):
        return res.structured_content
    return json.loads(res.content[0].text)


async def _session_run(cfg_path: Path, export_dir: Path):
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(command=sys.executable, args=["-m", "prokas_mcp"],
                                   env={"PROKAS_MCP_CONFIG": str(cfg_path), "PYTHONPATH": str(PROJECT_ROOT / "src")},
                                   cwd=str(PROJECT_ROOT))
    out = {}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            init = await s.initialize()
            out["server"] = init.server_info.name
            out["tools"] = sorted(t.name for t in (await s.list_tools()).tools)
            out["prompts"] = sorted(p.name for p in (await s.list_prompts()).prompts)
            out["status"] = _payload(await s.call_tool("connector_status", {}))
            out["file"] = _payload(await s.call_tool("run_report", {"z_code": "Z0705"}))
            out["denied"] = _payload(await s.call_tool("run_report", {"z_code": "Z0145"}))
            gui = _payload(await s.call_tool("run_report", {"z_code": "Z0245", "period": "letzte 7 Tage"}))
            out["gui"] = gui
            prompt = await s.get_prompt("prokas_export_runbook", {"job_id": gui["job_id"]})
            out["prompt"] = prompt.messages[0].content.text
            exp = Path(gui["expected_file"])
            write_txt("Z0245", exp.parent, exp.name.replace(".xlsx", ".txt"))
            out["ingest"] = _payload(await s.call_tool("ingest_export", {"job_id": gui["job_id"]}))
            out["morning"] = (await s.get_prompt("morning_check", {})).messages[0].content.text
    return out


def test_server_over_stdio(tmp_path):
    cfg_path = _config(tmp_path)
    (tmp_path / "Exporte").mkdir()
    write_txt("Z0705", tmp_path / "Exporte", "Z0705_nacht.txt")
    out = asyncio.run(asyncio.wait_for(_session_run(cfg_path, tmp_path / "Exporte"), 60))
    assert out["server"] == "prokas"
    assert len(out["tools"]) == 22 and {"run_report", "ingest_export", "margin_check", "stage_price_file"} <= set(out["tools"])
    assert out["prompts"] == ["margin_review", "morning_check", "prokas_export_runbook", "pull_report"]
    assert out["status"]["generation_allowed_now"] is True
    assert out["file"]["mode"] == "file" and out["file"]["row_count"] == 3
    assert out["denied"]["status"] == "error" and "nicht freigegeben" in out["denied"]["message"]
    assert out["gui"]["status"] == "needs_computer_use"
    assert out["gui"]["job_id"] in out["prompt"] and "Strg+A" in out["prompt"]
    assert out["ingest"]["status"] == "ok" and out["ingest"]["mode"] == "gui"
    assert "report_freshness" in out["morning"]
    blob = json.dumps(out, ensure_ascii=False, default=str)
    assert PERSON not in blob
    audit = list((tmp_path / "Logs").glob("audit-*.jsonl"))
    assert audit and len(audit[0].read_text(encoding="utf-8").splitlines()) >= 5
    assert "files" not in out["file"]  # full paths only in the audit log


def test_check_command(tmp_path):
    cfg_path = _config(tmp_path)
    (tmp_path / "Exporte").mkdir()
    write_txt("Z0127", tmp_path / "Exporte", "Z0127_nacht.txt")
    env = dict(os.environ, PROKAS_MCP_CONFIG=str(cfg_path), PYTHONPATH=str(PROJECT_ROOT / "src"))
    r = subprocess.run([sys.executable, "-m", "prokas_mcp", "check"], capture_output=True, text=True, env=env,
                       timeout=60, cwd=str(PROJECT_ROOT))
    assert "PROKAS-MCP Selbsttest" in r.stdout
    assert "[OK]  Z0127" in r.stdout
    assert "Nachtset" in r.stdout
    assert r.returncode == 0  # a missing nightly set is a warning, not a setup error
    assert "[!!]  Nachtset" in r.stdout
    r = subprocess.run([sys.executable, "-m", "prokas_mcp", "config"], capture_output=True, text=True, env=env, timeout=60)
    assert json.loads(r.stdout)["mcpServers"]["prokas"]["args"] == ["-m", "prokas_mcp"]
