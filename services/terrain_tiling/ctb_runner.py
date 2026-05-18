import subprocess
from typing import List, Optional


def run_cmd(argv: List[str], cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    cp = subprocess.run(argv, cwd=cwd, text=True, capture_output=True)
    if cp.returncode != 0:
        raise RuntimeError(
            f"Command failed (returncode={cp.returncode}) argv={argv} stderr={cp.stderr}"
        )
    return cp

