"""qqduty 本地管理命令行（tab 补全）。

用法：双击 qq_cli.bat 或  E:/python/python.exe -X utf8 qq_cli.py
命令：voice list / voice current / voice use <音色> / voice test <文本>
切换音色 = 写权重文件(gweight/sweight.txt) + 更新 voices.json + 杀掉在跑的 TTS（下次用时自动带新权重重启）。
"""
import cmd
import json
import os
import subprocess
import sys

NB_DIR = os.path.abspath(os.path.dirname(__file__))
VOICES_FILE = os.path.join(NB_DIR, "data", "voices.json")
TTS_DIR = r"T:\GPT-SoVITS-beta"

try:  # Windows 下 tab 补全靠 pyreadline3（没有也能用，只是没补全）
    import pyreadline3  # noqa: F401
except ImportError:
    pass


def load_voices() -> dict:
    with open(VOICES_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_voices(data: dict):
    with open(VOICES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def kill_tts():
    r = subprocess.run("netstat -ano", shell=True, capture_output=True, text=True,
                       encoding="gbk", errors="replace")
    out = r.stdout or ""
    pids = {ln.split()[-1] for ln in out.splitlines() if ":9880" in ln and "LISTENING" in ln}
    for pid in pids:
        subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True)
    return pids


class QQCli(cmd.Cmd):
    intro = "qqduty 管理台（tab 补全已开，help 看命令，exit 退出）"
    prompt = "qqduty> "

    def _voice_names(self) -> list:
        return sorted(load_voices().get("voices", {}))

    def do_voice(self, arg: str):
        args = arg.split(maxsplit=1)
        sub = args[0] if args else "current"
        data = load_voices()
        cur = data.get("current")
        if sub == "list":
            for name, v in data.get("voices", {}).items():
                mark = " ←当前" if name == cur else ""
                print(f"  {name:10s} {v.get('label', '')}{mark}")
        elif sub == "current":
            v = data.get("voices", {}).get(cur) or {}
            print(f"当前音色: {cur}（{v.get('label', '')}）")
            print(f"  GPT:    {v.get('gpt')}")
            print(f"  SoVITS: {v.get('sovits')}")
            print(f"  参考:   {v.get('ref_wav')}")
        elif sub == "use":
            name = args[1].strip() if len(args) > 1 else ""
            v = data.get("voices", {}).get(name)
            if not v:
                print(f"没有这个音色: {name!r}（voice list 看有哪些）")
                return
            with open(os.path.join(TTS_DIR, "gweight.txt"), "w", encoding="utf-8") as f:
                f.write(v["gpt"])
            with open(os.path.join(TTS_DIR, "sweight.txt"), "w", encoding="utf-8") as f:
                f.write(v["sovits"])
            data["current"] = name
            save_voices(data)
            killed = kill_tts()
            note = "，已杀掉在跑的TTS（下次语音自动带新音色重启）" if killed else ""
            print(f"已切换到 {name}（{v.get('label', '')}）{note}")
        elif sub == "test":
            text = args[1].strip() if len(args) > 1 else "这是当前音色的试听效果。"
            r = subprocess.run(
                [sys.executable, "-X", "utf8", os.path.join(NB_DIR, "qq_voice.py"),
                 "--scene", "0", "--text", text, "--dry-run"],
                capture_output=True, cwd=NB_DIR,
            )
            print(r.stdout.decode("utf-8", "replace").strip())
            print(f"（试听文件: {NB_DIR}\\tmp\\voice.wav）")
        else:
            print("用法: voice [list|current|use <音色>|test <文本>]")

    def complete_voice(self, text, line, begidx, endidx):
        before = line[:begidx].split()
        if len(before) == 1:  # 补子命令
            return [s for s in ["list", "current", "use", "test"] if s.startswith(text)]
        if len(before) == 2 and before[1] == "use":  # 补音色名
            return [n for n in self._voice_names() if n.startswith(text)]
        return []

    def do_exit(self, arg):
        return True

    do_quit = do_exit
    do_EOF = do_exit

    def emptyline(self):
        pass


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    QQCli().cmdloop()
