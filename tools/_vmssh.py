# -*- coding: utf-8 -*-
"""SSH helper: run command on VM, or pull files. Usage:
  python _vmssh.py "command"            -> run shell command
  python _vmssh.py --pull REMOTE LOCAL  -> sftp get
  python _vmssh.py --push LOCAL REMOTE  -> sftp put
"""
import os
import sys

import paramiko

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from local_secrets import load as load_local_secrets

_S = load_local_secrets()
HOST = str(_S.get("vm_host") or "")
USER = str(_S.get("vm_user") or "root")
PASSWORD = str(_S.get("vm_password") or "")


def client():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PASSWORD, timeout=10)
    return c


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    c = client()
    try:
        if args[0] == "--pull":
            sftp = c.open_sftp()
            sftp.get(args[1], args[2])
            print("pulled", args[1], "->", args[2])
        elif args[0] == "--push":
            sftp = c.open_sftp()
            sftp.put(args[1], args[2])
            print("pushed", args[1], "->", args[2])
        else:
            _, out, err = c.exec_command(args[0], timeout=120)
            o = out.read().decode("utf-8", "replace")
            e = err.read().decode("utf-8", "replace")
            rc = out.channel.recv_exit_status()
            if o:
                print(o)
            if e:
                print("[stderr]", e, file=sys.stderr)
            sys.exit(rc)
    finally:
        c.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
