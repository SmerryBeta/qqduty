"""本机凭据。真实值放在 data/local_secrets.json，该文件不入库。"""
import json
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(ROOT, "data", "local_secrets.json")


def load():
    try:
        with open(PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}
