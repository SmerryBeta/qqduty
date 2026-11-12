import argparse

from qoder_host_bridge import log, note_sent, send, send_file, send_image, sent_recently

p = argparse.ArgumentParser()
p.add_argument("--scene", required=True, help="群号或 private:QQ号")
p.add_argument("--text", default="", help="文字消息（与 --image/--file 可同用，先发文字）")
p.add_argument("--image", default="", help="本机图片路径，转 base64 发送")
p.add_argument("--file", default="", help="本机文件路径，上传为群文件（经 VM 中转）")
a = p.parse_args()

if not a.text and not a.image and not a.file:
    p.error("--text/--image/--file 至少给一个")

if a.text:
    if sent_recently(a.scene, a.text):  # 桥可能已把同文本发过（汇报双发事故 2026-11-09），跳过防刷屏
        log(f"dup skipped (qq_send) scene={a.scene} text={a.text[:60]!r}")
        print("DUP skipped")
    else:
        resp = send(a.scene, a.text)
        note_sent(a.scene, a.text)
        print(resp.get("status"), resp.get("retcode"), resp.get("wording", ""))
if a.image:
    resp = send_image(a.scene, a.image)
    print(resp.get("status"), resp.get("retcode"), resp.get("wording", ""))
if a.file:
    resp = send_file(a.scene, a.file)
    print(resp.get("status"), resp.get("retcode"), resp.get("wording", ""))
