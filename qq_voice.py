"""语音发送工具：把一句话用小风音色合成语音发进QQ群。

用法：
  qq_voice.py --scene 群号 --text "要说的内容"          # 合成并发送
  qq_voice.py --scene 群号 --text "..." --dry-run       # 只合成落盘不发（测试用）

流程：GPT-SoVITS(本机9880, 不在则自动拉起) → wav → ffmpeg转24k单声道 → pilk编silk → [CQ:record] 发群。
每一步都有宽裕超时和一次重试；任何一步失败打印原因并以非零码退出（调用方可回退文字）。
"""
import argparse
import os
import subprocess
import sys
import time

import qoder_host_bridge as B


def synth_with_retry(text: str) -> bytes | None:
    for attempt in (1, 2):
        t0 = time.time()
        try:
            silk = B.voice_silk(text)
            if silk:
                print(f"[tts] 第{attempt}次合成成功: {len(silk)}字节, 耗时{time.time()-t0:.0f}s")
                return silk
            print(f"[tts] 第{attempt}次合成返回空(耗时{time.time()-t0:.0f}s)")
        except Exception as e:
            print(f"[tts] 第{attempt}次合成异常(耗时{time.time()-t0:.0f}s): {e}")
        if attempt == 1:
            time.sleep(3)
    return None


def convert_with_retry() -> bool:
    for attempt in (1, 2):
        try:
            r = subprocess.run(
                [B.TTS_FFMPEG, "-y", "-i", B.VOICE_WAV, "-ar", "24000", "-ac", "1",
                 "-f", "s16le", B.VOICE_PCM],
                capture_output=True, timeout=180,
            )
            if r.returncode == 0 and os.path.getsize(B.VOICE_PCM) > 0:
                print(f"[ffmpeg] 第{attempt}次转换成功")
                return True
            print(f"[ffmpeg] 第{attempt}次转换失败 rc={r.returncode}")
        except subprocess.TimeoutExpired:
            print(f"[ffmpeg] 第{attempt}次转换超时(180s)")
        if attempt == 1:
            time.sleep(2)
    return False


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser()
    p.add_argument("--scene", required=True, help="群号或 private:QQ号")
    p.add_argument("--text", required=True, help="要念出来的内容")
    p.add_argument("--dry-run", action="store_true", help="只合成不发（自测用）")
    a = p.parse_args()

    if not B.tts_alive() and not B.tts_start():
        print("[fail] TTS服务起不来")
        sys.exit(2)

    a.text = B.clean_for_tts(a.text)  # 剥掉 CQ 码/@QQ号，免得被念出来
    if not a.text:
        print("[fail] 清洗后没有可念的内容")
        sys.exit(7)
    silk = synth_with_retry(a.text)
    if not silk:
        print("[fail] 合成失败")
        sys.exit(3)

    if a.dry_run:
        print(f"[dry-run] 合成完毕未发送, silk={len(silk)}字节, 试听: {B.VOICE_WAV}")
        return

    try:
        resp = B.send_voice(a.scene, silk)
        print(f"[send] {resp.get('status')} rc={resp.get('retcode')} {resp.get('wording', '')}")
        sys.exit(0 if resp.get("retcode") == 0 else 5)
    except Exception as e:
        print(f"[fail] 发送失败: {e}")
        sys.exit(6)


if __name__ == "__main__":
    main()
