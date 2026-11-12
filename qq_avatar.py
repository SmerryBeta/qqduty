import argparse
import json
import os
import time

from qoder_host_bridge import NB_DIR, fetch_avatar, log

p = argparse.ArgumentParser()
p.add_argument("--qq", required=True, help="要看的QQ号")
p.add_argument("--ask", default="", help="针对头像的重点问题（可选，如'这是哪个角色'）")
a = p.parse_args()
if not a.qq.isdigit():
    p.error("--qq 只认纯数字QQ号")

path = fetch_avatar(int(a.qq))
if not path:
    print("FAIL: 头像没拉下来")
    raise SystemExit(1)

# 看图不嵌套起 CLI（agent 肚子里嵌套各种挂）：写请求文件，桥看到后代跑（桥侧调用稳定），回答案文件
req_path = os.path.join(NB_DIR, "tmp", f"avatar_req_{a.qq}.json")
ans_path = os.path.join(NB_DIR, "tmp", f"avatar_ans_{a.qq}.json")
for f in (req_path, ans_path):
    try:
        os.remove(f)
    except OSError:
        pass
my_ts = time.time()
with open(req_path, "w", encoding="utf-8") as f:
    json.dump({"qq": a.qq, "path": path, "ask": a.ask, "ts": my_ts}, f, ensure_ascii=False)

desc = ""
deadline = time.time() + 300
while time.time() < deadline:
    time.sleep(3)
    try:
        with open(ans_path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        continue
    if abs(float(d.get("req_ts", 0)) - my_ts) < 1:
        desc = d.get("desc", "")
        break
    log(f"avatar stale answer ignored qq={a.qq}")

for f in (req_path, ans_path):
    try:
        os.remove(f)
    except OSError:
        pass

if desc:
    print(f"OK {path}")
    print(f"描述: {desc}")
else:
    print(f"OK {path}")
    print("描述失败: 这会儿看不了（桥没在跑或看图超时）；别用 Read 看图、别用 Python 切图分析，直接说看不了")
