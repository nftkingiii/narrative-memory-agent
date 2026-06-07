"""
utils.py — Narrative Memory Agent
Shared utilities — bgc CLI wrapper used by execution.py and fallback.py
"""

import os
import json
import shutil
import subprocess
from dotenv import load_dotenv

load_dotenv()

BITGET_API_KEY    = os.getenv("BITGET_API_KEY", "")
BITGET_SECRET_KEY = os.getenv("BITGET_SECRET_KEY", "")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "")


def bgc(module: str, command: str, **kwargs) -> dict | None:
    """
    Call a bgc CLI command and return parsed JSON.
    Windows-compatible: resolves bgc.cmd via shutil.which.
    """
    args = []
    for k, v in kwargs.items():
        args += [f"--{k}", str(v)]

    env = os.environ.copy()
    env["BITGET_API_KEY"]    = BITGET_API_KEY
    env["BITGET_SECRET_KEY"] = BITGET_SECRET_KEY
    env["BITGET_PASSPHRASE"] = BITGET_PASSPHRASE

    bgc_path = shutil.which("bgc") or shutil.which("bgc.cmd")

    if bgc_path:
        cmd = [bgc_path, module, command] + args
        use_shell = False
    else:
        cmd = f"bgc {module} {command} " + " ".join(args)
        use_shell = True

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            env=env, timeout=20, shell=use_shell
        )
        if result.returncode != 0:
            print(f"[bgc] error: {result.stderr.strip()}")
            return None
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        print(f"[bgc] timeout: {module} {command}")
        return None
    except json.JSONDecodeError as e:
        print(f"[bgc] JSON parse error: {e}")
        return None
    except Exception as e:
        print(f"[bgc] call failed: {e}")
        return None