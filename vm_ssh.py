import sys

import paramiko

from local_secrets import load as load_local_secrets

_S = load_local_secrets()
HOST = str(_S.get("vm_host") or "")
USER = str(_S.get("vm_user") or "root")
PWD = str(_S.get("vm_password") or "")


def run(cmd: str, timeout: int = 120) -> tuple[int, str]:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, 22, USER, PWD, timeout=10)
    try:
        _, o, e = c.exec_command(cmd, timeout=timeout)
        out = (o.read() + e.read()).decode("utf-8", "replace")
        return o.channel.recv_exit_status(), out
    finally:
        c.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "echo no-cmd"
    code, out = run(cmd, timeout=int(sys.argv[2]) if len(sys.argv) > 2 else 120)
    print(out)
    sys.exit(code)
