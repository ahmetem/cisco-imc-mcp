"""MCP server diagnostic - checks everything Claude Desktop checks.

Run this if Claude Desktop fails to start cisco-imc, and it will tell you
exactly what's wrong.

Usage:
    python _diag.py

Paths are derived from this script's location, so the file can be moved
without breaking the diagnostic.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

# All paths derived from this script's location for portability.
HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "cisco_imc_mcp.py"
ENV = HERE / ".env"
PY = Path(sys.executable)
CONFIG = Path(os.environ["APPDATA"]) / "Claude" / "claude_desktop_config.json"


def check(label, ok, detail=""):
    icon = "OK" if ok else "FAIL"
    print(f"[{icon}] {label}" + (f" - {detail}" if detail else ""))
    return ok


def main():
    all_ok = True

    all_ok &= check("Python exists", PY.exists(), str(PY))
    all_ok &= check("MCP script exists", SCRIPT.exists(), str(SCRIPT))
    all_ok &= check(".env exists", ENV.exists(), str(ENV))
    all_ok &= check("Claude config exists", CONFIG.exists(), str(CONFIG))

    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        servers = cfg.get("mcpServers", {})
        all_ok &= check(
            "Config has cisco-imc entry",
            "cisco-imc" in servers,
            f"servers: {list(servers.keys())}",
        )
        if "cisco-imc" in servers:
            entry = servers["cisco-imc"]
            cmd_path = Path(entry.get("command", ""))
            args = entry.get("args", [])
            script_path = Path(args[0]) if args else None
            all_ok &= check(
                "Config command path exists", cmd_path.exists(), str(cmd_path)
            )
            all_ok &= check(
                "Config script path exists",
                bool(script_path) and script_path.exists(),
                str(script_path),
            )
            all_ok &= check(
                "Config script matches local file",
                script_path and script_path.resolve() == SCRIPT.resolve(),
                f"config: {script_path} vs local: {SCRIPT}",
            )
    except Exception as e:
        all_ok &= check("Config parses as JSON", False, str(e))
        return

    proc = subprocess.run(
        [str(PY), "-c", "import mcp, httpx, pydantic, dotenv; print('ok')"],
        capture_output=True, text=True, timeout=15,
    )
    all_ok &= check(
        "Dependencies import",
        proc.returncode == 0 and "ok" in proc.stdout,
        proc.stderr.strip()[:200] if proc.returncode else "",
    )

    proc = subprocess.run(
        [str(PY), "-m", "py_compile", str(SCRIPT)],
        capture_output=True, text=True, timeout=15,
    )
    all_ok &= check(
        "Script compiles", proc.returncode == 0, proc.stderr.strip()[:300],
    )

    proc = subprocess.Popen(
        [str(PY), str(SCRIPT)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(HERE),
    )
    init = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "diag", "version": "1.0"},
        },
    }
    try:
        proc.stdin.write((json.dumps(init) + "\n").encode())
        proc.stdin.flush()
        out, err = proc.communicate(timeout=10)
        ok = b'"serverInfo"' in out and b'"cisco_imc_mcp"' in out
        all_ok &= check(
            "Server responds to MCP initialize", ok,
            err.decode("utf-8", "replace")[:300] if not ok else "",
        )
    except subprocess.TimeoutExpired:
        proc.kill()
        all_ok &= check("Server responds to MCP initialize", False, "timeout")
    except Exception as e:
        all_ok &= check("Server responds to MCP initialize", False, str(e))

    try:
        sys.path.insert(0, str(HERE))
        import asyncio
        from cisco_imc_mcp import imc_get_power_status, EmptyInput

        result = asyncio.run(imc_get_power_status(EmptyInput()))
        ok = "Power state:" in result and "Error" not in result
        all_ok &= check("IMC reachable & auth works", ok, result[:200])
    except Exception as e:
        all_ok &= check("IMC reachable & auth works", False, str(e))

    print()
    print("=" * 50)
    print("OVERALL:", "ALL OK" if all_ok else "PROBLEMS FOUND")
    print("=" * 50)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
