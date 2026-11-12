import base64
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque

import paramiko

from local_secrets import load as load_local_secrets

_S = load_local_secrets()
VM_HOST = str(_S.get("vm_host") or "")
VM_USER = str(_S.get("vm_user") or "root")
VM_PWD = str(_S.get("vm_password") or "")
REMOTE_LOG = "/opt/bot/nonebot/data/messages.jsonl"
API_BASE = f"http://{VM_HOST}:6700"
API_TOKEN = str(_S.get("api_token") or "")
SELF_ID = int(_S.get("self_id") or 0)
OWNER_ID = int(_S.get("owner_id") or 0)  # 主人：永远有念咒权限，且只有主人能授予/回收权限

NB_DIR = os.path.abspath(os.path.dirname(__file__))
BRIDGE_LOG = os.path.join(NB_DIR, "data", "qoder_bridge.log")
MEM_FILE = os.path.join(NB_DIR, "data", "bot_memory.md")
BRIDGE_IMG = os.path.join(NB_DIR, "data", "bridge_img.jpg")
QODER_CMD = str(_S.get("qoder_cmd") or "")

MEM_RE = re.compile(r"\[MEM\](.*?)\[/MEM\]", re.S)
# SnowLuma 的事件不带 NapCat 的 to_me 扩展字段（OneBot v11 标准里没有），
# @bot 只能自己从 raw 的 CQ:at 段认；QQ 号用 ,/]) 做右边界防前缀撞号
AT_SELF_RE = re.compile(rf"\[CQ:at,qq={SELF_ID}(?:,|\])")
REPLY_RE = re.compile(r"\[REPLY\](.*?)\[/REPLY\]", re.S)
FWD_ID_RE = re.compile(r"\[CQ:forward,id=([^\],]+)")  # 合并转发 resid：text 恒为空，raw 是唯一载体
AGENT_PREFIXES = ("巴啦啦能量！", "古娜拉黑暗之神！", "巴啦啦能量", "古娜拉黑暗之神")  # 正/邪双咒同权；带！在前优先剥掉，不带！的旧写法仍兼容


def _canon_la(s: str) -> str:
    """手机输入法常把 啦 打成 拉：咒头/咒文匹配前两边统一成 啦（逐字替换，长度不变）"""
    return s.replace("拉", "啦")


CANON_PREFIXES = tuple((_canon_la(p), p) for p in AGENT_PREFIXES)  # (归一化咒头, 原咒头)
OP_FILE = os.path.join(NB_DIR, "data", "bot_ops.json")  # 受权者（OP）名单，像 MC 的 ops.json
SEAL_FILE = os.path.join(NB_DIR, "data", "bot_sealed.json")  # 被小魔仙封印（禁言）的场景名单
CONFIG_FILE = os.path.join(NB_DIR, "data", "bot_config.json")  # 功能开关：撤回/截屏/追看/群活跃插话
SENT_LOG = os.path.join(NB_DIR, "data", "sent_log.jsonl")  # 发送流水（桥与 qq_send.py 共写），双发去重用
FEEDBACK_FILE = os.path.join(NB_DIR, "data", "feedback.md")  # @bot 反馈 内容记档
FEEDBACK_DIR = os.path.join(NB_DIR, "data", "feedback")  # 反馈带的图片落这里
TODO_FILE = os.path.join(NB_DIR, "data", "todos.json")  # 记账本：QQ号 → 该用户的 todo 列表
TODO_LOCK = threading.Lock()
TODO_MAX_ITEMS = 30
TODO_MAX_CHARS = 120
THINK_LO = 3.0  # 回复最低思考时间：从收到消息到开口，随机落在 3～5 秒
THINK_HI = 5.0
RECALL_FAIL = "哎哟我撤回不了了欸.."
SHOT_JPG = os.path.join(NB_DIR, "data", "bridge_shot.jpg")
# ---- 语音回复：本机 GPT-SoVITS（小风音色）合成 → silk 发群 ----
TTS_DIR = r"T:\GPT-SoVITS-beta"
TTS_API = "http://127.0.0.1:9880"
TTS_FFMPEG = os.path.join(TTS_DIR, "ffmpeg.exe")
# 小风: TTS_REF_WAV=logs\XiaoFeng\5-waw32k\vocal_风麻麻..._0000151680_0000346880.wav, TTS_PROMPT_TEXT=观众席，我们的观众已经迫不及待的想要看到比赛了。好的，现在我们来看第一轮。（权重备份在 gweight.txt.bak-xiaofeng/sweight.txt.bak-xiaofeng）
TTS_REF_WAV = os.path.join(TTS_DIR, r"logs\xxx\5-wav32k\vocal_Beta配音素材.mp3.reformatted.wav_10.wav_0000156160_0000340800.wav")
TTS_PROMPT_TEXT = "合适吗？要不然，我就把配音的工作交给AI吧。"
VOICE_WAV = os.path.join(NB_DIR, "tmp", "voice.wav")
VOICE_PCM = os.path.join(NB_DIR, "tmp", "voice.pcm")
VOICE_SILK = os.path.join(NB_DIR, "tmp", "voice.silk")
VOICES_FILE = os.path.join(NB_DIR, "data", "voices.json")  # 音色档案：current 指针 + 各音色的权重/参考音频（CLI 可切）
DARK_UNSEAL_KEY = "呜呼啦呼黑魔变身"  # 古娜拉黑暗之神+此咒 → 解除封印，允许本群聊天
LIGHT_SEAL_KEY = "呜卡拉卡小魔仙全身变"  # 巴啦啦能量+此咒 → 小魔仙封印，本群禁言
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

offset = 0
partial = b""
scenes: dict = {}  # scene -> deque[(到达时刻, rec)]，按群隔离的最近消息（#4 一群一实例）
last_consider: dict = {}  # scene -> 上次"要不要说几句"判断的时刻（插话冷却用）
pending_followups: list = []  # [{"scene","due","ts"}] 回复发完等几秒回头看有没有新话（#6）
AGENT_LOCK = threading.Lock()
agent_busy: dict = {}  # scene -> 正在跑的任务类型（agent=主人全权任务, search=路人仅搜索）
pending_agent: dict = {}  # scene -> [待跑的agent任务参数]（仅搜索占着时主人来了正经任务，排队等它跑完）


def scene_log(scene: str) -> deque:
    dq = scenes.get(scene)
    if dq is None:
        dq = deque(maxlen=300)
        scenes[scene] = dq
    return dq


def _scene_safe(scene: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]", "_", str(scene))


def inbox_path(scene: str) -> str:
    return os.path.join(NB_DIR, "tmp", f"agent_inbox_{_scene_safe(scene)}.md")


def _inbox_append(scene: str, who: str, text: str, note: str = ""):
    """agent 干活期间主人插的话写进收件箱，agent 每完成一步读一眼"""
    with open(inbox_path(scene), "a", encoding="utf-8") as f:
        f.write(f"- [{time.strftime('%H:%M:%S')}] {who}: {text}{note}\n")


def _inbox_clear(scene: str):
    try:
        os.remove(inbox_path(scene))
    except OSError:
        pass


def acquire_agent_slot(scene: str, kind: str = "agent") -> bool:
    with AGENT_LOCK:
        if scene in agent_busy:
            return False
        agent_busy[scene] = kind
        return True


def release_agent_slot(scene: str):
    with AGENT_LOCK:
        agent_busy.pop(scene, None)
    _spawn_pending_agent(scene)


def _spawn_pending_agent(scene: str):
    """占位空出来后，接排队的正经 agent 任务"""
    with AGENT_LOCK:
        if scene in agent_busy:
            return
        q = pending_agent.get(scene) or []
        if not q:
            return
        args = q.pop(0)
        if not q:
            pending_agent.pop(scene, None)
        agent_busy[scene] = "agent"
    threading.Thread(target=_run_agent_task, args=args, daemon=True, name=f"agent-{_scene_safe(scene)}").start()


def agent_busy_scene(scene: str) -> bool:
    with AGENT_LOCK:
        return scene in agent_busy


CHAT_LOCK = threading.Lock()
chat_busy: set = set()  # 正在生成聊天回复的场景
chat_pending: dict = {}  # scene -> [(到达时刻, rec)] 回复生成期间新来的@，攒一批合并回复


def chat_busy_scene(scene: str) -> bool:
    with CHAT_LOCK:
        return scene in chat_busy


def log(msg: str):
    line = f"{time.strftime('%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(BRIDGE_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def think_delay(received_at, now=None) -> float:
    """从收到消息到开口至少随机 3～5 秒；模型已经想更久就返回 0，不再追加"""
    try:
        received_at = float(received_at)
    except (TypeError, ValueError):
        return 0.0
    if received_at <= 0:
        return 0.0
    now = time.time() if now is None else now
    return max(0.0, random.uniform(THINK_LO, THINK_HI) - (now - received_at))


def wait_think(received_at):
    delay = think_delay(received_at)
    if delay > 0.05:
        log(f"think wait {delay:.1f}s")
        time.sleep(delay)


def _is_to_me(rec: dict) -> bool:
    """兼容 SnowLuma：to_me 字段恒为 false，@bot 从 raw 的 CQ:at 段自己判定，私聊视为 to_me"""
    if rec.get("to_me"):
        return True
    scene = rec.get("scene")
    if isinstance(scene, str) and scene.startswith("private:"):
        return True
    return bool(AT_SELF_RE.search(rec.get("raw") or ""))


def _is_spell_text(rec: dict) -> bool:
    """咒语头开头的消息：主循环放行进 handle，handle 内再细分（命令免@，任务仍需@）"""
    text = _canon_la((rec.get("text") or "").strip())
    return any(text.startswith(c) for c, _ in CANON_PREFIXES)


def read_memory() -> str:
    try:
        with open(MEM_FILE, encoding="utf-8") as f:
            return f.read().strip()[-3000:]
    except Exception:
        return ""


def append_memory(line: str):
    line = line.strip().replace("\n", " ")[:200]
    if not line:
        return
    with open(MEM_FILE, "a", encoding="utf-8") as f:
        f.write(f"- [{time.strftime('%m-%d')}] {line}\n")
    try:
        with open(MEM_FILE, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > 120:
            with open(MEM_FILE, "w", encoding="utf-8") as f:
                f.writelines(lines[-100:])
    except Exception:
        pass


def context_text(scene: str) -> str:
    out = []
    for _, d in scenes.get(scene) or ():
        if d.get("text"):
            out.append(f"{d.get('t', '')} {d.get('sender', '')}({d.get('user_id', '')}) : {d['text'][:80]}")
        elif _has_forward(d):
            out.append(f"{d.get('t', '')} {d.get('sender', '')}({d.get('user_id', '')}) : [聊天记录]")
    return "\n".join(out[-12:])


def http_json(url: str, payload: dict | None = None, timeout: int = 15) -> dict:
    data = None
    headers = {"Authorization": f"Bearer {API_TOKEN}"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def send(scene: str, text: str):
    if scene.startswith("private:"):
        payload = {"user_id": int(scene.split(":")[1]), "message": text}
        action = "send_private_msg"
    else:
        payload = {"group_id": int(scene), "message": text}
        action = "send_group_msg"
    return http_json(f"{API_BASE}/{action}", payload)


DEDUP_WIN = 300  # 同场景同文本 5 分钟内视为重复


def _dedup_key(text: str) -> str:
    return re.sub(r"\s+", "", text)[:200]


def _dedup_trackable(text: str) -> bool:
    k = _dedup_key(text)
    return bool(k) and not k.startswith(("[CQ:image", "[CQ:record"))


def sent_recently(scene: str, text: str) -> bool:
    """双发去重：2026-11-09 内存卡事件 agent 用进度命令自己把最终汇报发了，桥又把 [REPLY] 发一遍，群里 ABAB。
    桥与 qq_send.py 共写 sent_log，发送前查同场景同文本是否刚发过"""
    if not _dedup_trackable(text):
        return False
    try:
        with open(SENT_LOG, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("scene") == scene and d.get("key") == _dedup_key(text) and time.time() - d.get("ts", 0) < DEDUP_WIN:
                    return True
    except OSError:
        pass
    return False


def note_sent(scene: str, text: str):
    if not _dedup_trackable(text):
        return
    with open(SENT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"scene": scene, "key": _dedup_key(text), "ts": time.time()}, ensure_ascii=False) + "\n")
    try:
        with open(SENT_LOG, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > 2000:
            with open(SENT_LOG, "w", encoding="utf-8") as f:
                f.writelines(lines[-1000:])
    except OSError:
        pass


def send_multi(scene: str, msgs: list[str]):
    """多条消息连发，模拟人手动连发的节奏"""
    last = None
    for i, m in enumerate(msgs):
        if sent_recently(scene, m):
            log(f"dup skipped [{i + 1}/{len(msgs)}] scene={scene} text={m[:60]!r}")
            continue
        try:
            resp = send(scene, m)
            last = resp
            note_sent(scene, m)
            log(f"sent[{i + 1}/{len(msgs)}] scene={scene} rc={resp.get('retcode')} text={m[:60]!r}")
        except Exception as e:
            log(f"send failed [{i + 1}]: {e}")
            break
        if i < len(msgs) - 1:
            time.sleep(0.9)
    return last


def load_ops() -> set[int]:
    try:
        with open(OP_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {int(x) for x in data.get("ops", [])} - {OWNER_ID}
    except Exception:
        return set()


def save_ops(ops: set[int]):
    ops = set(ops) - {OWNER_ID}  # 主人权限是内置的，不进名单
    with open(OP_FILE, "w", encoding="utf-8") as f:
        json.dump({"ops": sorted(ops)}, f, ensure_ascii=False, indent=2)


def load_unsealed() -> set[str]:
    """已解禁聊天的群（白名单）；群默认封印，念解禁咒才开"""
    try:
        with open(SEAL_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("unsealed", []))
    except Exception:
        return set()


def save_unsealed(scenes: set[str]):
    with open(SEAL_FILE, "w", encoding="utf-8") as f:
        json.dump({"unsealed": sorted(scenes)}, f, ensure_ascii=False, indent=2)


def scene_chat_ok(scene: str) -> bool:
    """群默认封印（AI 聊天不说话），解禁咒开通；私聊场景不受限"""
    return str(scene).startswith("private:") or str(scene) in load_unsealed()


DEFAULT_CONFIG = {
    "recall": {"enabled": True},
    "screenshot": {"enabled": True},
    "search": {"enabled": True},
    "voice": {"enabled": True},
    "followup": {"enabled": True, "wait_sec": 5},
    "passive": {"enabled": True, "window_min": 10, "threshold": 30, "min_gap_min": 10},
    "forward": {"enabled": True, "max_images": 6},  # 合并转发展开：@bot/引用链里的聊天记录拉开给AI看
    "groups": {},  # 按群覆盖，如 {"123456": {"passive": {"enabled": false}}}
}


def load_config() -> dict:
    """功能开关（#9）：文件缺键/损坏时回落默认值，永远能拿到完整结构"""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        merged = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_CONFIG.items()}
        for k, v in data.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
        return merged
    except Exception:
        return {k: dict(v) for k, v in DEFAULT_CONFIG.items()}


def _norm_spell(body: str) -> str:
    """咒文归一化：标点/空白全忽略，拉/啦同字，只看字序"""
    return _canon_la(re.sub(r"[，,、。.\s！!？?~～·]", "", body))


def try_seal_command(scene: str, user_id: int, prefix: str, task: str) -> bool:
    """群级聊天开关：群默认封印；黑魔变身=解禁本群聊天，小魔仙全身变=重新封上。仅主人有效"""
    key = _norm_spell(task)
    unseal = prefix.startswith("古娜拉黑暗之神") and key == _canon_la(DARK_UNSEAL_KEY)
    seal = prefix.startswith("巴啦啦能量") and key == _canon_la(LIGHT_SEAL_KEY)
    if not (unseal or seal):
        return False
    if user_id != OWNER_ID:
        log(f"seal spell denied user={user_id} scene={scene}")
        send_multi(scene, ["封印术只认主人的手印"])
        return True
    active = load_unsealed()
    if unseal:
        active.add(scene)
        save_unsealed(active)
        log(f"chat unsealed scene={scene}")
        send_multi(scene, ["呜呼啦呼——黑魔变身！", "封印解除，这个群又能聊了"])
    else:
        active.discard(scene)
        save_unsealed(active)
        log(f"chat sealed scene={scene}")
        send_multi(scene, ["呜卡拉卡——小魔仙，全身变！", "黑暗力量已被封印，本群闭嘴"])
    return True


def try_op_command(scene: str, user_id: int, body: str) -> bool:
    """主人专用：巴啦啦能量 op/deop/oplist 管理念咒权限，返回 True 表示已处理"""
    if user_id != OWNER_ID:
        return False
    parts = body.split()
    cmd = parts[0].lower() if parts else ""
    args = parts[1:]
    if cmd in ("op", "授权"):
        if len(args) != 1 or not args[0].isdigit():
            send_multi(scene, ["用法：巴啦啦能量 op <QQ号>"])
        else:
            ops = load_ops()
            ops.add(int(args[0]))
            save_ops(ops)
            log(f"op granted {args[0]} (by owner)")
            send_multi(scene, [f"已授予 {args[0]} 撤回权限", "让它悠着点用"])
        return True
    if cmd in ("deop", "收权"):
        if len(args) != 1 or not args[0].isdigit():
            send_multi(scene, ["用法：巴啦啦能量 deop <QQ号>"])
        else:
            ops = load_ops()
            ops.discard(int(args[0]))
            save_ops(ops)
            log(f"op revoked {args[0]} (by owner)")
            send_multi(scene, [f"已回收 {args[0]} 的撤回权限"])
        return True
    if cmd in ("oplist", "权限表", "授权列表"):
        ops = load_ops()
        names = "、".join(str(o) for o in sorted(ops)) or "（无）"
        send_multi(scene, [f"当前可帮撤回：{names}", f"主人 {OWNER_ID} 自带权限"])
        return True
    return False


def fetch_reply(rec: dict) -> dict | None:
    """取被回复的那条消息（#1）：文字/发送者/图片/合并转发一并带回来；取失败也返回标记，不静默"""
    reply_id = rec.get("reply_id")
    if not reply_id:
        return None
    try:
        resp = http_json(f"{API_BASE}/get_msg", {"message_id": int(reply_id)})
        data = resp.get("data") or {}
        sender = data.get("sender") or {}
        segs = data.get("message")
        if isinstance(segs, str):
            fid = _fwd_resid(segs)
            fwd_text, fwd_imgs = expand_forward(fid, str(rec.get("scene") or "")) if fid else ("", [])
            return {"text": segs.strip()[:300], "sender": sender.get("nickname") or str(sender.get("user_id") or ""),
                    "image_url": None, "image_urls": fwd_imgs[:6], "forward_text": fwd_text,
                    "is_self": int(sender.get("user_id") or 0) == SELF_ID}
        texts, image_urls, fwd_text, fwd_imgs = [], [], "", []
        for seg in segs or []:
            d = seg.get("data") or {}
            if seg.get("type") == "text":
                texts.append(d.get("text") or "")
            elif seg.get("type") == "image":
                u = d.get("url")
                if u and u not in image_urls:
                    image_urls.append(u)
            elif seg.get("type") == "forward" and str(d.get("id") or ""):
                fwd_text, fwd_imgs = expand_forward(str(d["id"]), str(rec.get("scene") or ""))
        return {"text": "".join(texts).strip()[:300], "sender": sender.get("nickname") or str(sender.get("user_id") or ""),
                "image_url": image_urls[0] if image_urls else None, "image_urls": (image_urls + fwd_imgs)[:6],
                "forward_text": fwd_text, "is_self": int(sender.get("user_id") or 0) == SELF_ID}
    except Exception as e:
        log(f"get_msg(reply) failed: {e}")
        return {"text": "", "sender": "", "image_url": None, "is_self": False, "failed": True}


def _reply_note(rep: dict | None) -> str | None:
    if rep is None:
        return None
    if rep.get("failed"):
        return "（被回复的消息取不到了）"
    who = "自己" if rep.get("is_self") else (rep.get("sender") or "某人")
    body = rep.get("text") or ("（图片）" if rep.get("image_url") else "（空消息）")
    if rep.get("forward_text"):
        body = (body + "\n" if rep.get("text") else "") + "「合并转发聊天记录」\n" + rep["forward_text"][:1500]
    return f"{who}发的: {body}"


# ---- 合并转发聊天记录展开：@bot/引用链/咒语任务里的 forward 卡片拉开给 AI 看 ----
FORWARD_MAX_DEPTH = 3    # 嵌套展开层数上限（QQ 端发送侧同限）
FORWARD_MAX_CALLS = 12   # get_forward_msg 总调用数熔断
FORWARD_MAX_NODES = 120  # 展开总节点数熔断
FWD_NODE_TEXT_MAX = 400  # 单节点文本截断
FWD_BLOCK_MAX = 4000     # 整块文本截断


def _has_forward(rec: dict) -> bool:
    return bool(FWD_ID_RE.search(rec.get("raw") or ""))


def _fwd_resid(s: str) -> str | None:
    m = FWD_ID_RE.search(s or "")
    return m.group(1) if m else None


def _fwd_name(sender: dict) -> str:
    """节点发送者只显昵称：合并转发里节点的QQ号不可靠——2026-11-11实测群聊记录的号整包失真
    （9节点群记录两个不同的人全是转发者的号），昵称才可靠；别让AI拿假号认人/点评马甲"""
    name = _clean_name(str(sender.get("card") or "")) or _clean_name(str(sender.get("nickname") or ""))
    if not name or name == "QQ用户":
        return "未知"
    return name


def _fwd_quoted(d: dict) -> str:
    """转发节点里 reply 段的内嵌引用摘要：SnowLuma 附 data.message（原文段数组/串）；qq 一并略去（同上不可靠）"""
    inner = d.get("message")
    texts = [inner] if isinstance(inner, str) else [
        str((seg.get("data") or {}).get("text") or "") for seg in inner or []
        if isinstance(seg, dict) and seg.get("type") == "text"]
    quoted = re.sub(r"\s+", " ", "".join(texts)).strip()
    return f"[引用: {quoted[:60]}]" if quoted else "[引用]"


def _fwd_node(node: dict, depth: int, st: dict) -> str:
    st["nodes"] += 1
    ind = "  " * (depth - 1)
    try:
        ts = node.get("time") or 0
        when = time.strftime("%m-%d %H:%M", time.localtime(int(ts))) if ts else "时间未知"
        segs = node.get("message")
        if isinstance(segs, str):
            segs = [{"type": "text", "data": {"text": segs}}]
        parts, nested = [], []
        for seg in segs or []:
            d = seg.get("data") or {}
            t = seg.get("type")
            if t == "text":
                parts.append(str(d.get("text") or ""))
            elif t == "image":
                u = d.get("url")
                if u and u not in st["images"]:
                    if len(st["images"]) < st["max_images"]:
                        st["images"].append(u)
                    else:
                        st["dropped"] += 1
                parts.append("[图片]")
            elif t == "at":
                parts.append(f"@{d.get('qq')}")
            elif t == "forward" and str(d.get("id") or ""):
                try:
                    nested.append(_fwd_fetch(str(d["id"]), depth + 1, st))
                except Exception as e:
                    log(f"forward inner fetch failed resid={d['id']}: {e}")
                    nested.append(f"[内层聊天记录拉取失败: {str(e)[:60]}]")
            elif t == "reply":
                parts.append(_fwd_quoted(d))
            elif t in ("record", "video"):
                parts.append("[语音]" if t == "record" else "[视频]")
            elif t == "file":
                parts.append(f"[文件:{str(d.get('name') or d.get('file') or '')[:30]}]")
            elif t in ("json", "xml"):
                parts.append("[卡片消息]")
            elif t == "face":
                parts.append("[表情]")
            elif t:
                parts.append(f"[{t}]")
        body = re.sub(r"\s+", " ", " ".join(parts)).strip()[:FWD_NODE_TEXT_MAX]
        return "\n".join([f"{ind}[{when}] {_fwd_name(node.get('sender') or {})}: {body}"] + nested)
    except Exception as e:
        log(f"forward node parse failed: {e}")
        return f"{ind}[该条记录解析失败]"


def _fwd_fetch(resid: str, depth: int, st: dict) -> str:
    """拉一层合并转发并渲染；retcode≠0 抛给调用方（外层转占位），内层由 _fwd_node 就地占位"""
    if resid in st["seen"]:
        return "[重复的聊天记录，已略过]"
    if depth > FORWARD_MAX_DEPTH:
        return "[嵌套超过3层，未展开]"
    if st["calls"] >= FORWARD_MAX_CALLS or st["nodes"] >= FORWARD_MAX_NODES:
        log(f"forward expand aborted calls={st['calls']} nodes={st['nodes']} resid={resid}")
        return "[记录太多，其余未展开]"
    st["seen"].add(resid)
    st["calls"] += 1
    resp = http_json(f"{API_BASE}/get_forward_msg", {"id": resid}, timeout=20)
    if resp.get("retcode") not in (0, None):
        raise RuntimeError(f"retcode={resp.get('retcode')} {str(resp.get('wording') or '')[:60]}")
    data = resp.get("data")
    # get_group_msg_history 出过 data 直接是 list 的异常（日志 11-10 实证），这里同样防御
    msgs = data.get("messages") if isinstance(data, dict) else (data if isinstance(data, list) else None)
    if not isinstance(msgs, list):
        raise RuntimeError("响应格式异常")
    ind = "  " * (depth - 1)
    head = f"【合并转发聊天记录 {len(msgs)}条】" if depth == 1 else f"{ind}└─【内嵌聊天记录 {len(msgs)}条】"
    lines = [head]
    for m in msgs:
        if st["nodes"] >= FORWARD_MAX_NODES:
            lines.append(f"{ind}[记录太多，其余未展开]")
            break
        lines.append(_fwd_node(m, depth, st))
    if depth == 1 and st["dropped"]:
        lines.append(f"（另有{st['dropped']}张图超出上限未附）")
    return "\n".join(lines)


def expand_forward(resid: str, scene: str) -> tuple[str, list]:
    """合并转发展开总入口：配置关了返回空（行为与今天一致），拉挂了给占位文本，绝不向调用方抛异常"""
    conf = load_config()
    cfg = conf["forward"]
    g = ((conf.get("groups") or {}).get(str(scene)) or {}).get("forward") or {}
    if cfg.get("enabled") is False or g.get("enabled") is False:
        return "", []
    st = {"seen": set(), "calls": 0, "nodes": 0, "images": [], "dropped": 0,
          "max_images": int(g.get("max_images", cfg.get("max_images", 6)) or 6)}
    try:
        text = _fwd_fetch(resid, 1, st)
        log(f"forward expand scene={scene} nodes={st['nodes']} calls={st['calls']} imgs={len(st['images'])}")
        return text[:FWD_BLOCK_MAX], st["images"]
    except Exception as e:
        log(f"forward expand failed resid={resid} scene={scene}: {e}")
        return f"（合并转发内容拉取失败: {str(e)[:80]}）", []


def forward_notes(recs: list) -> str:
    """主消息里的合并转发逐条展开；图挂 rec["_fwd_images"] 供 gather_images 收集（必须在它之前调用）"""
    blocks = []
    for r in recs:
        fid = _fwd_resid(r.get("raw") or "")
        if not fid:
            continue
        text, imgs = expand_forward(fid, str(r.get("scene") or ""))
        if not text:
            continue
        if imgs:
            r["_fwd_images"] = imgs
        blocks.append(f"（{r.get('sender')}({r.get('user_id')})发来的合并转发）\n{text}")
    return "\n".join(blocks)


AT_QQ_RE = re.compile(r"\[CQ:at,qq=(\d+)")


def _clean_name(s: str) -> str:
    """群名片常有多行花体艺术字/控制字符，压成一行可打印文本"""
    return re.sub(r"\s+", " ", "".join(ch for ch in s if ch.isprintable())).strip()


def member_name(scene: str, qq: int) -> str:
    """QQ号 → 群名片/昵称（@提及解析用）；私聊场景退化为陌生人资料"""
    try:
        if str(scene).startswith("private:"):
            resp = http_json(f"{API_BASE}/get_stranger_info", {"user_id": qq})
        else:
            resp = http_json(f"{API_BASE}/get_group_member_info", {"group_id": int(scene), "user_id": qq})
        d = resp.get("data") or {}
        card = _clean_name(d.get("card") or "")
        return card or _clean_name(d.get("nickname") or "")
    except Exception as e:
        log(f"member_info failed qq={qq}: {e}")
        return ""


def mentions_note(recs: list) -> str:
    """消息里 @ 到的人（含 @bot）解析成 群名片(QQ号) 写进提示词；
    VM 侧 text 把 CQ 码剥掉了，模型光看 "@熊熊" 永远对不上 QQ 号"""
    seen, parts = set(), []
    for r in recs:
        for qq in AT_QQ_RE.findall(r.get("raw") or ""):
            qq = int(qq)
            if qq == SELF_ID or qq in seen:
                continue
            seen.add(qq)
            name = member_name(str(r.get("scene")), qq)
            parts.append(f"{name}({qq})" if name else str(qq))
    return f"【消息里@到的人】{'、'.join(parts[:8])}\n" if parts else ""


def _clean_child_env() -> dict:
    """起 qodercli 的环境要剥掉 QODER_*：agent shell 里被注入 QODER_AGENT_SDK_DAEMON/QODERCLI_RUNTIME_PACKAGING
    等 worker 变量，嵌套再起 CLI 会把自己当 daemon 工人卡死（2026-11-09 420s 0字节实锤）；只留配置目录"""
    keep = {"QODER_CONFIG_DIR", "QODERCN_CONFIG_DIR"}
    return {k: v for k, v in os.environ.items() if not (k.upper().startswith("QODER") and k not in keep)}


def vision_describe(path: str, question: str = "", timeout: int = 420, model: str = "GLM-5.3-Flash") -> str:
    """本机图片 → 文字描述：起一次带 --attachment 的模型调用。agent 的 Read 工具看不了图，
    附件这条路才通（2026-11-09 验证：同一张头像附件路看清、Read 路失明导致 agent 切图失控）。
    model="" 用 CLI 默认模型——GLM-5.3-Flash 看个别图会卡死（2026-11-10 实证：毒图默认模型29s出活）"""
    prompt = ("请看这张图片并用中文详细描述：人物外观（性别、发色发型、瞳色、服饰、饰品）、画风（真人照片/二次元/Q版等）、"
              "画面里的其他元素；认得出是哪个角色/人物就直接说名字和依据。只输出描述本身。"
              + (f"\n另外重点回答：{question}" if question else ""))
    child_env = _clean_child_env()
    args = ["cmd", "/c", QODER_CMD, "-p"]
    if model:
        args += ["-m", model]
    args += ["-n", "QQ看图", "-w", NB_DIR,
             "--tools", "", "--permission-mode", "default", "--reasoning-effort", "low",
             "--attachment", path]
    t0 = time.time()
    out = ""
    try:
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=child_env)
        try:
            out_b, _ = proc.communicate(prompt.encode("utf-8"), timeout=timeout)
            out = out_b.decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
            try:
                out_b, _ = proc.communicate(timeout=15)
                out = (out_b or b"").decode("utf-8", "replace")
            except Exception:
                out = ""
    except Exception as e:
        log(f"vision spawn failed: {e}")
        return ""
    m = REPLY_RE.search(out)
    desc = m.group(1).strip() if m else ""
    if not desc:
        for line in out.splitlines()[::-1]:
            line = line.strip().strip('"').strip("`")
            if line and len(line) >= 4:
                desc = line
                break
    desc = re.sub(r"\s+", " ", desc)[:600]
    log(f"vision describe {os.path.basename(path)} cost={time.time()-t0:.0f}s chars={len(desc)}")
    return desc


def _at_field(mns: str) -> str:
    """mentions_note → 日志用的紧凑字段；解析结果必须进控制台，别只在提示词里看不见"""
    at = mns.replace("【消息里@到的人】", "").strip()
    return f" at={at}" if at else ""


AVATAR_REQ_RE = re.compile(r"\[AVATAR[:：]\s*(\d+)\]", re.I)
AVATAR_URLS = [
    "https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640",
    "https://q.qlogo.cn/headimg_dl?dst_uin={qq}&spec=640&img_type=jpg",
]


def fetch_avatar(qq: int) -> str | None:
    """QQ号 → 头像jpg路径（qq_avatar.py 命令行与聊天模式的按需拉取共用）"""
    dest = os.path.join(NB_DIR, "tmp", f"avatar_{qq}.jpg")
    for tpl in AVATAR_URLS:
        try:
            req = urllib.request.Request(tpl.format(qq=qq), headers={"User-Agent": "Mozilla/5.0"})
            data = OPENER.open(req, timeout=30).read()
            if len(data) > 500:
                import io

                from PIL import Image
                Image.open(io.BytesIO(data)).convert("RGB").save(dest, "JPEG", quality=90)
                log(f"avatar ready qq={qq} -> {dest}")
                return dest
        except Exception as e:
            log(f"avatar fetch failed qq={qq}: {e}")
    return None


def avatar_requests(replies: list[str]) -> list[int]:
    """从模型回复里抠 [AVATAR:QQ号] 请求；出现在任何一行都算（模型被要求单独成行，但别依赖它守规矩）"""
    qqs: list[int] = []
    for m in replies:
        for q in AVATAR_REQ_RE.findall(m):
            q = int(q)
            if q not in qqs:
                qqs.append(q)
    return qqs[:3]


def avatar_sheet(scene: str, qqs: list[int]) -> tuple[str | None, str]:
    """按请求拉头像：一张直接用，多张横排拼图+名字标签；返回 (图路径, 提示词用的说明)"""
    items = [(q, p) for q in qqs if (p := fetch_avatar(q))]
    if not items:
        return None, ""
    names = []
    for qq, _ in items:
        nm = member_name(scene, qq)
        names.append(f"{nm}({qq})" if nm else str(qq))
    label = "、".join(names) + ("，从左到右" if len(names) > 1 else "")
    if len(items) == 1:
        return items[0][1], label
    try:
        from PIL import Image, ImageDraw, ImageFont

        thumbs = []
        for _, p in items:
            im = Image.open(p).convert("RGB")
            im.thumbnail((360, 360))
            thumbs.append(im)
        th = max(im.height for im in thumbs)
        w = sum(im.width + 12 for im in thumbs) + 12
        sheet = Image.new("RGB", (w, th + 50), (24, 24, 24))
        dr = ImageDraw.Draw(sheet)
        try:
            font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 22)
        except OSError:
            font = ImageFont.load_default()
        x = 12
        for im, nm in zip(thumbs, names):
            sheet.paste(im, (x, 12))
            dr.text((x, th + 20), nm[:12], fill=(240, 240, 240), font=font)
            x += im.width + 12
        dest = os.path.join(NB_DIR, "tmp", f"avatar_sheet_{_scene_safe(scene)}.jpg")
        sheet.save(dest, "JPEG", quality=88)
        return dest, label
    except Exception as e:
        log(f"avatar sheet failed: {e}")
        return items[0][1], names[0]


def send_image(scene: str, path: str):
    """发本机图片（#12）：SnowLuma 发送口支持 base64:// 图片段"""
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return send(scene, f"[CQ:image,file=base64://{b64}]")


def send_file(scene: str, path: str):
    """发本机文件：SnowLuma 的群文件接口只认 VM 上的路径，先走 SFTP 中转再调接口"""
    name = os.path.basename(path)
    remote = f"/tmp/bot_file_{int(time.time())}_{name}"
    client, sftp = connect()
    try:
        sftp.put(path, remote)
        if scene.startswith("private:"):
            action = "upload_private_file"
            payload = {"user_id": int(scene.split(":")[1]), "file": f"file://{remote}", "name": name}
        else:
            action = "upload_group_file"
            payload = {"group_id": int(scene), "file": f"file://{remote}", "name": name}
        return http_json(f"{API_BASE}/{action}", payload, timeout=120)
    finally:
        try:
            sftp.remove(remote)
        except Exception:
            pass
        client.close()


def send_voice(scene: str, silk: bytes):
    return send(scene, f"[CQ:record,file=base64://{base64.b64encode(silk).decode()}]")


def _parse_voice(task: str) -> str | None:
    """咒头剥掉后剩"语音！内容"→ 返回内容（可为空串）；不是语音指令返回 None"""
    m = re.match(r"^语音[！!，,、\s]*(.*)$", _canon_la(task), re.S)
    return m.group(1).strip() if m else None


def tts_alive() -> bool:
    try:
        OPENER.open(urllib.request.Request(TTS_API), timeout=2).read(1)
    except urllib.error.HTTPError:
        return True  # 有 HTTP 响应（如 400）即服务在
    except Exception:
        return False
    return True


def tts_start() -> bool:
    """按需拉起本机 GPT-SoVITS 服务（加载模型约 15 秒）"""
    log("tts: starting GPT-SoVITS api")
    try:
        env = dict(os.environ, OMP_NUM_THREADS="8", MKL_NUM_THREADS="8")  # 宿主机高负载时 torch 线程超订会把推理拖慢百倍(2026-11-05 踩过)
        with open(os.path.join(NB_DIR, "tmp", "gptsovits_api.log"), "ab") as lf:
            subprocess.Popen(
                [os.path.join(TTS_DIR, "runtime", "python.exe"), "api.py", "-a", "127.0.0.1", "-p", "9880"],
                cwd=TTS_DIR, stdout=lf, stderr=subprocess.STDOUT, env=env,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
    except Exception as e:
        log(f"tts start failed: {e}")
        return False
    for _ in range(30):
        time.sleep(2)
        if tts_alive():
            log("tts: api ready")
            return True
    log("tts: api not ready in 60s")
    return False


def load_voice() -> dict:
    """当前音色档案（权重/参考音频/文字）；voices.json 缺失或损坏时回落内置 Beta 档"""
    try:
        with open(VOICES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        v = (data.get("voices") or {}).get(data.get("current") or "")
        if v:
            return v
    except Exception:
        pass
    return {"ref_wav": "logs/xxx/5-wav32k/vocal_Beta配音素材.mp3.reformatted.wav_10.wav_0000156160_0000340800.wav",
            "prompt_text": "合适吗？要不然，我就把配音的工作交给AI吧。"}


def voice_silk(text: str) -> bytes | None:
    """文字 → 当前音色 wav → 24k 单声道 pcm → silk；任一步失败返回 None（调用方回退文字）"""
    if not tts_alive() and not tts_start():
        return None
    vc = load_voice()
    payload = {
        "text": text, "text_language": "zh",
        "refer_wav_path": os.path.join(TTS_DIR, vc["ref_wav"]),
        "prompt_text": vc["prompt_text"], "prompt_language": "zh",
    }
    req = urllib.request.Request(TTS_API + "/", data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with OPENER.open(req, timeout=300) as r:  # 空闲时4-15秒; 宿主机高负载时可能拖很久, 给足余量, 失败反正有文字兜底
        wav = r.read()
    if not wav.startswith(b"RIFF"):
        log(f"tts unexpected head: {wav[:8]!r}")
        return None
    with open(VOICE_WAV, "wb") as f:
        f.write(wav)
    r1 = subprocess.run([TTS_FFMPEG, "-y", "-i", VOICE_WAV, "-ar", "24000", "-ac", "1", "-f", "s16le", VOICE_PCM],
                        capture_output=True, timeout=180)
    if r1.returncode != 0:
        log(f"ffmpeg failed: {r1.stderr.decode('utf-8', 'replace')[:120]}")
        return None
    import pilk
    pilk.encode(VOICE_PCM, VOICE_SILK, pcm_rate=24000)
    with open(VOICE_SILK, "rb") as f:
        return f.read()


def take_screenshot() -> str | None:
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab(all_screens=True)
        if img.mode != "RGB":
            img = img.convert("RGB")
        for q in (70, 45):
            img.save(SHOT_JPG, "JPEG", quality=q)
            if os.path.getsize(SHOT_JPG) < 3 * 1024 * 1024:
                break
        return SHOT_JPG
    except Exception as e:
        log(f"screenshot failed: {e}")
        return None


def _is_op(user_id: int) -> bool:
    return user_id == OWNER_ID or user_id in load_ops()


def try_screenshot_command(rec: dict, scene: str, user_id: int, text: str) -> bool:
    """@bot 说"截屏"：截宿主机屏幕发群里（#12），仅主人/OP"""
    if text != "截屏" or not _is_to_me(rec):
        return False
    if not load_config()["screenshot"]["enabled"]:
        return False
    if not _is_op(user_id):
        return False  # 非权限者当普通聊天，AI 自己接话
    path = take_screenshot()
    if path:
        log(f"sent screenshot scene={scene} size={os.path.getsize(path)}")
        send_image(scene, path)
    else:
        send_multi(scene, ["截屏失败了，再试一次？"])
    return True


def _is_recall_cmd(rec: dict) -> bool:
    text = (rec.get("text") or "").strip().strip("！!。.，,~～ ")
    return bool(rec.get("reply_id")) and text == "撤回"


def try_recall_command(rec: dict, scene: str, user_id: int) -> bool:
    """回复 bot 的消息说"撤回"→ 删那条（#2），仅主人/OP；只删得了 bot 自己发的"""
    if not _is_recall_cmd(rec):
        return False
    if not load_config()["recall"]["enabled"]:
        log(f"recall disabled, ignored scene={scene}")
        return True
    if not _is_op(user_id):
        log(f"recall denied scene={scene} user={user_id}")
        return True  # 无权限：静默（不教陌生人这个功能）
    reply_id = int(rec.get("reply_id"))
    try:
        resp = http_json(f"{API_BASE}/get_msg", {"message_id": reply_id})
        sender_id = int((resp.get("data") or {}).get("sender", {}).get("user_id") or 0)
        if sender_id != SELF_ID:
            return False  # 撤的不是 bot 的消息：可能只是在跟别人说话，不插嘴也不走AI
        r = http_json(f"{API_BASE}/delete_msg", {"message_id": reply_id})
        if r.get("status") == "ok" or r.get("retcode") == 0:
            log(f"recalled msg={reply_id} scene={scene}")
        else:
            log(f"recall failed rc={r.get('retcode')} {r.get('wording')}")
            send_multi(scene, [RECALL_FAIL])
    except Exception as e:
        log(f"recall error: {e}")
        if _is_to_me(rec):  # 明确是冲着 bot 来的才报失败，否则静默
            send_multi(scene, [RECALL_FAIL])
    return True


def try_feedback_command(rec: dict, scene: str, user_id: int, text: str) -> bool:
    """@bot 反馈 <内容>（可带图/纯图）：原样记档不走 AI；封印中的群也能用"""
    t = (text or "").strip()
    if not t.startswith("反馈") or not _is_to_me(rec):
        return False
    content = t[2:].strip(" ：:，,、")
    imgs = rec.get("images") or []
    if not content and not imgs:
        return False  # 只念了"反馈"俩字：当普通聊天
    os.makedirs(FEEDBACK_DIR, exist_ok=True)
    saved = []
    for i, u in enumerate(imgs[:6]):
        name = f"{time.strftime('%Y%m%d_%H%M%S')}_u{user_id}_{i}.jpg"
        if download_image(u, os.path.join(FEEDBACK_DIR, name)):
            saved.append(name)
    note = f"｜图: {'、'.join(saved)}" if saved else (f"｜原图x{len(imgs)}没下下来" if imgs else "")
    with open(FEEDBACK_FILE, "a", encoding="utf-8") as f:
        f.write(f"- [{time.strftime('%Y-%m-%d %H:%M:%S')}] scene={scene} {rec.get('sender')}({user_id}): {content}{note}\n")
    log(f"feedback recorded scene={scene} user={user_id} text={content[:40]!r} imgs={len(saved)}/{len(imgs)}")
    send_multi(scene, [f"{rec.get('sender')}，反馈收到，记下了"])
    return True


def try_help_command(rec: dict, scene: str, user_id: int, text: str) -> bool:
    """@bot 帮助：功能清单（2026-11-10 群反馈加）；首条带称呼，防去重误吞不同人的帮助请求"""
    if (text or "").strip() != "帮助" or not _is_to_me(rec):
        return False
    who = rec.get("sender") or "同学"
    send_multi(scene, [
        f"{who}，学会的活都在这了：\n@我+话 闲聊接话（带图我也看得见）\n问“XX的头像长啥样” → 我去瞅一眼再形容\n合并转发的聊天记录@我（或引用它@我）→ 我能展开看里面聊了啥",
        "回复我某条消息说“撤回” → 删那条（要权限）\n@我 截屏 → 看宿主机屏幕（要权限）\n@我 反馈 内容 → 提建议给我家主人，带图也行",
        f"{who}，记账本是每人一本：\n@我 记账 内容 → 记一条\n@我 记账本 → 看自己记了啥\n@我 划掉 序号 → 删掉那条",
        "主人专属：回复消息说“转发到群 群名”（我连发的整段一起带走）、“发给群 群名：内容”\n咒语“巴啦啦能量！/古娜拉黑暗之神！”+任务 → 干大活（仅我家主人），任务里带“语音！”就用语音回\n@我 帮助 → 本菜单",
    ])
    log(f"help sent scene={scene} user={user_id}")
    return True


def _load_todo_book() -> dict:
    try:
        with open(TODO_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_todo_book(data: dict):
    os.makedirs(os.path.dirname(TODO_FILE), exist_ok=True)
    tmp = TODO_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TODO_FILE)


def _todo_items(data: dict, user_id: int) -> list:
    items = data.get(str(int(user_id)))
    if not isinstance(items, list):
        return []
    return [x for x in items if isinstance(x, str) and x.strip()]


def _todo_say(scene: str, who: str, msg: str):
    """首行带称呼和时间，防同文 5 分钟去重把第二个人的回执吞掉"""
    send_multi(scene, [f"{who}，{msg}（{time.strftime('%H:%M:%S')}）"])


TODO_ADD_RE = re.compile(r"^记账(?!本)\s*(.*)$", re.S)
TODO_DONE_RE = re.compile(r"^(?:划掉|完成)\s*(\d+)\s*$")


def try_todo_command(rec: dict, scene: str, user_id: int, text: str) -> bool:
    """@bot 记账本：每人一个 key，存自己的 todo 列表。纯代码，不走 AI，封印群也能用"""
    if not _is_to_me(rec) or not user_id:
        return False
    t = (text or "").strip()
    who = rec.get("sender") or "同学"
    listing = t in ("记账本", "我的记账")
    done = TODO_DONE_RE.match(t)
    adding = TODO_ADD_RE.match(t)
    if not listing and not done and not adding:
        return False

    def show(items: list):
        if not items:
            _todo_say(scene, who, "记账本是空的。@我 记账 内容 就能记")
            return
        body = "\n".join(f"{i}. {item}" for i, item in enumerate(items, 1))
        _todo_say(scene, who, f"你的记账本 {len(items)} 条：\n{body}")

    with TODO_LOCK:
        data = _load_todo_book()
        items = _todo_items(data, user_id)
        if done:
            n = int(done.group(1))
            if n < 1 or n > len(items):
                _todo_say(scene, who, f"没有第{n}条，现在一共 {len(items)} 条")
                return True
            removed = items.pop(n - 1)
            data[str(int(user_id))] = items
            _save_todo_book(data)
            _todo_say(scene, who, f"划掉了第{n}条：{removed}")
            log(f"todo done scene={scene} user={user_id} n={n}")
            return True
        if listing or not (adding.group(1) or "").strip():
            show(items)
            log(f"todo list scene={scene} user={user_id} n={len(items)}")
            return True
        content = adding.group(1).strip()
        if len(content) > TODO_MAX_CHARS:
            content = content[:TODO_MAX_CHARS]
        if len(items) >= TODO_MAX_ITEMS:
            _todo_say(scene, who, f"记账本满了（{TODO_MAX_ITEMS}条），先 @我 划掉 序号")
            return True
        items.append(content)
        data[str(int(user_id))] = items
        _save_todo_book(data)
        _todo_say(scene, who, f"记下了（第{len(items)}条）：{content}")
        log(f"todo add scene={scene} user={user_id} n={len(items)} text={content[:40]!r}")
        return True


GROUP_LIST_CACHE = {"ts": 0.0, "list": []}


def resolve_group(name: str):
    """群名/群号 → group_id；命中返回 int，失败返回给主人看的说明（不静默跳过）"""
    name = (name or "").strip()
    if name.isdigit():
        return int(name)
    if time.time() - GROUP_LIST_CACHE["ts"] > 300:
        try:
            resp = http_json(f"{API_BASE}/get_group_list")
            GROUP_LIST_CACHE["list"] = resp.get("data") or []
            GROUP_LIST_CACHE["ts"] = time.time()
        except Exception as e:
            return f"群列表这会儿拉不下来：{e}"
    exact = [g for g in GROUP_LIST_CACHE["list"] if str(g.get("group_name")) == name]
    if len(exact) == 1:
        return int(exact[0]["group_id"])
    fuzzy = [g for g in GROUP_LIST_CACHE["list"]
             if name in str(g.get("group_name")) or str(g.get("group_name")) in name]
    hits = exact if exact else fuzzy
    if len(hits) == 1:
        return int(hits[0]["group_id"])
    if not hits:
        return f"没找到叫“{name}”的群，我进的群里都对不上，给个全名或群号试试"
    return "有好几个群对得上：" + "、".join(f"{g.get('group_name')}({g.get('group_id')})" for g in hits[:5]) + "，说全名或直接给群号"


def _rebuild_msg(entry: dict) -> str | None:
    """群历史/消息记录 → 可重新发送的文本（text+@段拼接）；只有图没字返回 None（图另行处理）"""
    segs = entry.get("message")
    if isinstance(segs, str):
        return segs.strip() or None
    parts = []
    for seg in segs or []:
        d = seg.get("data") or {}
        if seg.get("type") == "text" and (d.get("text") or "").strip():
            parts.append(d["text"].strip())
        elif seg.get("type") == "at":
            parts.append(f"[CQ:at,qq={d.get('qq')}]")
    return "\n".join(parts) if parts else None


FWD_RE = re.compile(r"^(?:把.{0,8}?)?转发到群?(.+)$")
SEND_TO_RE = re.compile(r"^发给群(.+?)[：:]\s*(.+)$", re.S)


def try_forward_command(rec: dict, scene: str, user_id: int, text: str) -> bool:
    """主人专用：回复消息说"转发到群 X"（bot 自己连发的整段一起带走）或"发给群 X：内容" → 定向发到指定群；
    非主人不拦截（AI 当玩笑接），群名解析失败/歧义都明说"""
    t = (text or "").strip()
    m_send = SEND_TO_RE.match(t)
    m_fwd = FWD_RE.match(t) if rec.get("reply_id") else None
    if not m_send and not m_fwd:
        return False
    if user_id != OWNER_ID or not _is_to_me(rec):
        return False
    if m_send:
        target = resolve_group(m_send.group(1))
        if isinstance(target, str):
            send_multi(scene, [target])
            return True
        resp = send(str(target), m_send.group(2).strip())
        if resp.get("retcode") == 0:
            log(f"forward sent->scene={target} by owner text={m_send.group(2)[:40]!r}")
            send_multi(scene, [f"已发到群 {target}"])
        else:
            send_multi(scene, [f"发群 {target} 失败了：{resp.get('wording') or resp.get('retcode')}"])
        return True
    # 转发被回复的消息：主人转发别人的消息带署名；回复的是 bot 自己的就连发整段一起转
    try:
        resp = http_json(f"{API_BASE}/get_msg", {"message_id": int(rec["reply_id"])})
        src = resp.get("data") or {}
        start_seq = int(src.get("message_seq") or 0)
        grp = int(src.get("group_id") or 0)
        src_self = int((src.get("sender") or {}).get("user_id") or 0) == SELF_ID
    except Exception as e:
        send_multi(scene, [f"被回复的那条取不到了：{e}"])
        return True
    entries = [src]
    if src_self and grp and start_seq:
        try:
            hist = http_json(f"{API_BASE}/get_group_msg_history", {"group_id": grp, "message_seq": start_seq + 8, "count": 10})
            after = [m for m in (hist.get("data") or {}).get("messages") or []
                     if int(m.get("message_seq") or 0) > start_seq]
            for m in sorted(after, key=lambda x: int(x.get("message_seq") or 0)):
                if int((m.get("sender") or {}).get("user_id") or 0) != SELF_ID:
                    break
                body = _rebuild_msg(m)
                if not body:
                    break
                entries.append(m)
                if len(entries) >= 6:
                    break
        except Exception as e:
            log(f"forward burst fetch failed: {e}")  # 整段取不全就只转被回复那条，不挡事
    out = []
    for i, m in enumerate(entries):
        body = _rebuild_msg(m)
        if not body:
            continue
        if i == 0 and not src_self:
            who = (m.get("sender") or {}).get("nickname") or "TA"
            body = f"转 {who} 的话：{body}"
        out.append(body[:500])
    if not out:
        send_multi(scene, ["那条消息没文字可转（纯图/空）"])
        return True
    target = resolve_group(m_fwd.group(1))
    if isinstance(target, str):
        send_multi(scene, [target])
        return True
    last = send_multi(str(target), out)
    if last and last.get("retcode") == 0:
        log(f"forward burst->{target} n={len(out)} by owner")
        send_multi(scene, [f"已把 {len(out)} 条转到群 {target}"])
    else:
        send_multi(scene, [f"转群 {target} 失败了：{(last or {}).get('wording') or '没发出去'}"])
    return True


def download_image(url: str, dest: str = BRIDGE_IMG) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with OPENER.open(req, timeout=30) as r:
            content = r.read()
        if not content:
            return False
        with open(dest, "wb") as f:
            f.write(content)
        return True
    except Exception as e:
        log(f"image download failed: {e}")
        return False


def scene_img_path(scene: str) -> str:
    """按群分图片临时文件：多群 agent 并行时互不覆盖"""
    return os.path.join(NB_DIR, "data", f"bridge_img_{_scene_safe(scene)}.jpg")


def compose_images(paths: list, cols: int = 2, thumb: int = 640) -> str:
    """多图拼成一张网格图（CLI 的 --attachment 只有一个位子）；从左到右、从上到下"""
    from PIL import Image

    ims = []
    for p in paths:
        try:
            im = Image.open(p).convert("RGB")
            im.thumbnail((thumb, thumb))
            ims.append(im)
        except Exception as e:
            log(f"compose skip {os.path.basename(p)}: {e}")
    if not ims:
        return ""
    if len(ims) == 1:
        return paths[0]
    gap = 10
    w = max(im.width for im in ims)
    h = max(im.height for im in ims)
    rows = (len(ims) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * w + gap * (cols + 1), rows * h + gap * (rows + 1)), (24, 24, 24))
    for i, im in enumerate(ims):
        r, c = divmod(i, cols)
        sheet.paste(im, (gap + c * (w + gap), gap + r * (h + gap)))
    out = os.path.join(NB_DIR, "tmp", f"img_sheet_{int(time.time())}.jpg")
    sheet.save(out, "JPEG", quality=85)
    return out


def gather_images(recs: list, reps: list) -> tuple[bool, str]:
    """把一批消息 + 被回复消息里的图全收下（多图支持）：单图直接用，多图拼网格；返回 (有没有图, 路径)"""
    urls = []
    for r in recs:
        for u in (r.get("images") or []) + (r.get("_fwd_images") or []):
            if u and u not in urls:
                urls.append(u)
    for rep in reps:
        if not rep:
            continue
        for u in (rep.get("image_urls") or ([rep["image_url"]] if rep.get("image_url") else [])):
            if u and u not in urls:
                urls.append(u)
    if not urls:
        return False, ""
    scene = str(recs[0].get("scene") or "") if recs else ""
    base = scene_img_path(scene)
    paths = []
    for i, u in enumerate(urls[:6]):
        p = base if i == 0 else base.replace(".jpg", f"_{i}.jpg")
        if download_image(u, p):
            paths.append(p)
        else:
            log(f"image download missed: {u[:80]}")
    if not paths:
        return False, ""
    if len(paths) == 1:
        return True, paths[0]
    sheet = compose_images(paths)
    if not sheet:
        return True, paths[0]
    return True, sheet


def run_qoder(scene: str, sender: str, user_id: int, text: str, has_image: bool, is_agent: bool = False, is_owner: bool = True, replied: str | None = None, consider: bool = False, img_path: str = BRIDGE_IMG, search: bool = False, voice: bool = False, mentions: str = "", avatar_note: str = "", forward_note: str = "", model: str = "GLM-5.3-Flash") -> tuple[list[str], str]:
    fwd_line = ("【合并转发聊天记录】\n（说明：合并转发里的QQ号不可靠，展开时只标了昵称，认人只看昵称；"
                "不要点评「一人分饰多角/换马甲」之类的身份疑点，那是记录显示的毛病不是真马甲）\n" + forward_note + "\n") if forward_note else ""
    if consider:
        img_line = "【最近聊天里的图片】已随附（多张会拼成一图，按从左到右、从上到下顺序）。\n" if has_image else ""
        prompt = f"""你在潜水看一个QQ群的聊天（代管账号 {SELF_ID}），系统隔一阵让你看看要不要插几句话。

【你的长期记忆】
{read_memory() or "（暂无）"}

【本群最近聊天记录】
{context_text(scene)}
{img_line}
【规矩】
- 口语化中文，像普通群友的自然口吻，绝不暴露 AI 身份
- 想说就自然接话（可以玩梗、补充、吐槽）；话题不熟、没话找话、需要实时信息（新闻/比分/价格）的时候：不说话，别不懂装懂
- 少 @ 人；绝不编造具体数据和日期

【输出格式】
想说：用 [REPLY] 和 [/REPLY] 包住，换行拆成 1-3 条短消息
不想说：只输出 [NOREPLY]"""
    elif is_agent:
        if is_owner:
            power_note = "- 直接把事办了：该读文件就读、该跑命令就跑，不要只给建议不动手"
            risk_note = "- 有破坏性风险的操作（删除、覆盖、改系统配置）先停在无害步骤，汇报时说清风险等本人确认"
        else:
            power_note = "- 直接把事办了：该读文件就读、该跑命令就跑，不要只给建议不动手"
            risk_note = "- 本次由受权者（非主人）下达：破坏性操作（删除、覆盖、改系统配置、动账号凭据）一律禁止执行，停在无害步骤并汇报，等主人确认"
        py_exe = sys.executable.replace("\\", "/")
        root_fwd = NB_DIR.replace("\\", "/")
        shot = root_fwd + "/tmp/agent_shot.png"
        progress_cmd = f"{py_exe} -X utf8 {root_fwd}/qq_send.py --scene {scene} --text '一句话进度'"
        img_cmd = f"{py_exe} -X utf8 {root_fwd}/qq_send.py --scene {scene} --image {shot}"
        file_cmd = f"{py_exe} -X utf8 {root_fwd}/qq_send.py --scene {scene} --file '文件路径'"
        avatar_cmd = f"{py_exe} -X utf8 {root_fwd}/qq_avatar.py --qq QQ号"
        voice_note = "\n- 本任务的最终汇报会被合成成语音念出来：汇报写成口语短句（60字以内）、不@人不放链接；要@下令人提醒就放进进度消息里" if voice else ""
        replied_line = f"被回复的消息：{replied}\n" if replied else ""
        img_line = "【任务图片】消息里的图已随附（多张会拼成一图，按从左到右、从上到下顺序）。\n" if has_image else ""
        prompt = f"""管理咒语（{'/'.join(AGENT_PREFIXES[:2])}）已激活：你本轮拥有完整的 agent 能力（读写文件、执行命令），运行在这台 Windows 宿主机上（工作目录 {NB_DIR}，可操作任意路径）。

【你的长期记忆】
{read_memory() or "（暂无）"}

【任务】场景: {scene} | 下令人: {sender}({user_id}) | 任务: {text}
{replied_line}{fwd_line}{mentions}{img_line}
【要求】
{power_note}
{risk_note}{voice_note}
- 全程让下令人看到你在动，别长时间沉默：开工先发一条简短的"收到，开始办"；之后每完成一个关键步骤（或发现重要情况）就发一条一句话进度。发进度直接执行这条命令（Git Bash，scene 已填好，把单引号里的字换成你要说的话）：
  {progress_cmd}
  进度也是口语化中文、不暴露 AI 身份；几分钟就能干完的小活发条开工的就行别刷屏，整轮中间进度最多 6 条；这条命令发失败就跳过继续干，最后汇报里提一句即可。**最终汇报绝不要用这条命令发**：汇报只写在下面的 [REPLY] 里由系统统一发出，你再用命令发一遍群里会收到两遍
- 主人可能中途插话补充指示：每完成一个关键步骤就读一眼收件箱 tmp/agent_inbox_{_scene_safe(scene)}.md（cat 它），有新内容就并入当前任务直接照办（不用重新确认，下令人不变），已处理的行把它从文件里删掉
- 要给下令人看屏幕/本机图片时：先截屏（PowerShell 直接执行）：
  powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; $b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds; $m=New-Object System.Drawing.Bitmap $b.Width,$b.Height; $g=[System.Drawing.Graphics]::FromImage($m); $g.CopyFromScreen(0,0,0,0,$b.Size); $m.Save('{shot}')"
  然后执行（换掉 --image 路径即可发任意本机图片文件）：
  {img_cmd}
- 要发文件（文档/压缩包等）时（先传VM再走群文件接口，低频使用别刷屏）：
  {file_cmd}
- 要看某个 QQ 号的头像（认人、看看对方是谁/什么角色）时执行（把 QQ号 换成目标，任务栏和"@到的人"里都有现成的号，要问具体问题可在末尾加 `--ask '问题'`）：
  {avatar_cmd}
  它会拉头像并调看图模型**直接打印头像的详细描述**（外观/画风/疑似角色），照打印出来的描述回答即可。**别用 Read 看图**（这环境 Read 看不了图）、**别用 Python 切图逐像素分析、别传外部识图接口硬猜**。这条命令**整个任务只跑一次**：等它跑完（要 1-3 分钟，正常）；打印"描述失败"或跑了 4 分钟还没出结果就直接汇报"这会儿看不了头像"收尾，**不重试、不换别的办法**
- 消息里 @ 人的写法：原样写 [CQ:at,qq=QQ号]（进度命令的 --text 里和最终汇报里都能用，单引号内直接写就行）；最终汇报的第一条开头就 @ 下令人（QQ号在上面任务栏里），好让对方收到提醒
- 做完简短汇报：做了什么、结果如何；口语化中文，不暴露 AI 身份
- 汇报用 [REPLY] 和 [/REPLY] 包住，里面用换行拆成 1-3 条短消息
- 可另起一行输出 [MEM]一句话记忆[/MEM] 记录任务关键结论（可选）"""
    elif search:
        replied_line = f"被回复的消息：{replied}\n" if replied else ""
        prompt = f"""有人@了你代管的 QQ 账号（{SELF_ID}），让你帮忙查个东西。你这轮只有联网搜索权限，没有读写文件、执行命令等任何本地能力。

【任务】下令人: {sender}({user_id})
{replied_line}{fwd_line}{mentions}任务: {text}

【要求】
- 先用网络搜索查证再回答；查不到就直说查不到，绝不编造数据、日期、链接
- 口语化中文，像普通群友顺手帮忙查了一下，不暴露 AI 身份
- 附一两条关键来源网址（纯文本直接贴）
- 用 [REPLY] 和 [/REPLY] 包住回复，换行拆成 1-3 条短消息
- 可另起一行输出 [MEM]一句话记忆[/MEM]（仅当值得长期记住）"""
    else:
        if avatar_note:  # 头像二轮：附带的就是头像本身，别再按"消息附带图片"描述
            img_note = ""
        elif has_image:
            img_note = "【附带图片】消息带图，已随附（多张会拼成一图，按从左到右、从上到下顺序）；回复可参考图片内容（除非对方问图，别硬描述图）。"
        else:
            img_note = ""
        voice_note = "【重要】你的回复会被直接合成成语音念出来：写口语短句（1-2句、60字以内）；绝不@人、绝不写[CQ:at]之类的标记、绝不提QQ号、不放链接、不用颜文字和英文缩写——这些都会被当成字念出来，很怪。" if voice else ""
        at_line = "" if voice else "要 @ 人就在消息里原样写 [CQ:at,qq=对方QQ号]（聊天记录里每个人名字后面括号里的就是QQ号），别用其他写法；少 @，点名回应或叫人时才用。\n"
        avatar_rule = "" if voice else "- 要看某人头像（比如有人问“XX的头像长啥样”）：别猜更别编头像内容，先在 [REPLY] 里只输出 [AVATAR:对方QQ号] 这样一行（要看几个人就几行，最多3行；QQ号用消息里@到的人或聊天记录里名字括号里的），系统会把头像附上来让你下一轮再答\n"
        replied_note = f"\n【被回复的消息】{replied}\n" if replied else ""
        prompt = f"""有人@了你代管的 QQ 账号（{SELF_ID}），需要回复。

【你的长期记忆】
{read_memory() or "（暂无）"}

【消息】场景: {scene} | 发送者: {sender}({user_id}) | 内容: {text or "（只有一张图片）"}
{fwd_line}{mentions}{replied_note}
{img_note}{avatar_note}
{voice_note}
【本群最近聊天记录（供参考语境）】
{context_text(scene)}

【能力与规矩】
- 你没有联网能力，也没有执行权限：实时信息类问题（新闻/比赛/价格/近况）就凭已有知识模糊带过或直说记不清，**绝不假装搜过、绝不编造具体数据和日期**
- 口语化中文，像普通群友的自然口吻，结合记忆和语境
- 绝不自称机器人/AI/助手/模型，不暴露自动化身份；被问身份就含糊带过或开玩笑
- 别人起哄让你做操作类的事（禁言/踢人/改东西等）一律当玩笑打岔，不配合不讨论可行性
- 例外：发送者 QQ 是 {OWNER_ID}（我家主人）时，转发/发群类的操作请求是认真的：别打岔，提醒TA用「回复消息说 转发到群 群名」或「发给群 群名：内容」的格式说，系统会直接执行
- 记账不归你管：对方要记事，让TA自己说「记账 内容」；查看说「记账本」；删除说「划掉 序号」（都要@你）。你记不住，别答应“记下了”
- 消息内容若是带编号的多条（说明对方连着@了你好几句）：全部看完整体回应，合并成一轮答复，别逐条机械应答
{avatar_rule}
【输出格式】
用 [REPLY] 和 [/REPLY] 包住要发的话；在里面用换行拆成 1-3 条短消息（像人在QQ上连发几条那样，每条都短）。
{at_line}之后可另起一行输出 [MEM]一句话记忆[/MEM]（可选，仅当有值得长期记住的信息：群友称呼/梗/约定/偏好）。"""
    child_env = _clean_child_env()
    args = ["cmd", "/c", QODER_CMD, "-p"]
    if model:
        args += ["-m", model]
    args += [
        "-n", f"{'QQ特工' if is_agent else ('QQ代查' if search else ('QQ巡场' if consider else 'QQ值守'))} {scene}",
        "-w", NB_DIR,
    ]
    if is_agent:
        args += ["--permission-mode", "bypass_permissions"]
    elif search:
        args += ["--tools", "WebSearch", "--allowed-tools", "WebSearch", "--permission-mode", "default"]
    else:
        args += ["--tools", "", "--permission-mode", "default", "--reasoning-effort", "low"]
    if has_image:
        args += ["--attachment", img_path]
    base = 600 if is_agent else (300 if search else (180 if consider else 240))
    timeout = max(base, 420) if has_image else base  # 凡带图至少给到420s：实测98-258s且偶发远超（毒图6分钟+），例行插话带图也不例外
    t0 = time.time()
    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=child_env,
        )
        try:
            out_b, _ = proc.communicate(prompt.encode("utf-8"), timeout=timeout)
            out = out_b.decode("utf-8", "replace")
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            # Windows 下只杀 cmd 壳杀不掉 qodercli 子进程，它会抓着输出管道拖几分钟（2026-11-09 240s拖成360s），必须树杀
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
            try:
                out_b, _ = proc.communicate(timeout=15)
                out = (out_b or b"").decode("utf-8", "replace")
            except Exception:
                out = ""
            rc = "timeout"
    except Exception as e:
        out = ""
        rc = f"spawn failed: {e}"

    mem = ""
    match = MEM_RE.search(out)
    if match:
        mem = match.group(1).strip()
        append_memory(mem)
    cleaned = MEM_RE.sub("", out)
    unclosed = re.search(r"\[MEM\]([^\n]*)", cleaned)
    if unclosed:
        mem = unclosed.group(1).strip()
        append_memory(mem)
        cleaned = re.sub(r"\[MEM\][^\n]*", "", cleaned)

    replies: list[str] = []
    m = REPLY_RE.search(cleaned)
    if m:
        for ln in m.group(1).splitlines():
            ln = ln.strip().strip('"').strip("`")
            if ln and "[MEM]" not in ln:
                replies.append(ln[:400])
    if not replies and not consider:  # 模型没用标记时的兜底：取最后一条有效行（巡场模式不许兜底，没标记就是不说话）
        for line in cleaned.splitlines()[::-1]:
            line = line.strip().strip('"').strip("`")
            if line and len(line) >= 2 and "[MEM]" not in line:
                replies = [line[:400]]
                break
    replies = replies[:5]

    with open(BRIDGE_LOG, "a", encoding="utf-8") as f:
        f.write(
            f"\n===== {time.strftime('%m-%d %H:%M:%S')} scene={scene} sender={sender} "
            f"mode={'agent' if is_agent else 'chat'} cost={time.time()-t0:.0f}s rc={rc}\n"
            f"reply={[r[:80] for r in replies]}\nmem=[{mem[:200]}]\n"
        )
    return replies, str(rc)


def _run_agent_task(rec: dict, scene: str, sender: str, user_id: int, text: str):
    """后台跑 agent 任务：主循环不阻塞；期间主人插话进收件箱，由任务自己吸收"""
    received_at = rec.get("_recv_at") or 0
    try:
        rep = fetch_reply(rec)
        fwd = forward_notes([rec])
        has_image, img = gather_images([rec], [rep])
        replied = _reply_note(rep)
        mns = mentions_note([rec])
        log(f"handle scene={scene} sender={sender} mode=agent text={text[:40]!r} img={has_image} reply={bool(replied)}{_at_field(mns)}")
        replies, rc = run_qoder(scene, sender, user_id, text, has_image, is_agent=True, is_owner=True, replied=replied, img_path=img, voice=bool(rec.get("_voice")), mentions=mns, forward_note=fwd)
        if not replies:
            log(f"no reply text (rc={rc})")
            if rc == "timeout":  # 卡死装死最伤体验（毒图任务常这样）：明说一声
                try:
                    wait_think(received_at)
                    send_multi(scene, ["这单卡住了，多半是那张图太费眼，缓缓再喊我一次"])
                except Exception as e:
                    log(f"timeout notice failed: {e}")
            return
        wait_think(received_at)
        if rec.get("_voice"):
            _deliver_voice_or_text(scene, replies)  # 带咒的语音任务：最终汇报念出来
        else:
            send_multi(scene, replies)
    except Exception as e:
        log(f"agent task error: {e}")
    finally:
        release_agent_slot(scene)


def _chat_batch_text(recs: list) -> str:
    """一批@消息拼成编号清单；太多只留最近的"""
    if len(recs) > 15:
        recs = recs[-15:]
        head = f"（较早的几条略，下面是最近的{len(recs)}条）\n"
    else:
        head = ""
    lines = []
    for i, r in enumerate(recs, 1):
        t = (r.get("text") or "").strip() or ("（聊天记录）" if _has_forward(r) else "（图片）")
        lines.append(f"{i}. {r.get('sender')}({r.get('user_id')}): {t[:120]}")
    return head + "\n".join(lines)


def _run_chat_worker(item: tuple, scene: str):
    """聊天回复后台线程：回复期间新来的@攒起来，发完当前回复后合并成一次模型调用一起回；语音不参与合并"""
    batch = [item]
    try:
        while True:
            if any(r.get("_voice") for _, r in batch):
                for ts, r in batch:  # 混入语音时逐条回，避免语音/文字形态搅在一起
                    if r.get("_voice"):
                        _chat_reply_voice(scene, r, ts)
                    else:
                        _chat_reply(scene, [(ts, r)])
            else:
                _chat_reply(scene, batch)
            with CHAT_LOCK:
                pend = chat_pending.pop(scene, [])
            if not pend:
                return
            batch = pend
            log(f"chat batch merge scene={scene} n={len(batch)}")
    finally:
        with CHAT_LOCK:
            chat_busy.discard(scene)


def _chat_reply(scene: str, items: list):
    recs = [r for _, r in items]
    received_at = min((ts for ts, _ in items), default=0)
    first = recs[0]
    last_ts = items[-1][0]
    text = _chat_batch_text(recs) if len(recs) > 1 else (first.get("text") or "").strip()
    replied_parts = []
    reps = [fetch_reply(r) if r.get("reply_id") else None for r in recs]
    for r, rep in zip(recs, reps):
        note = _reply_note(rep)
        if note:
            replied_parts.append(note if len(recs) == 1 else f"{r.get('sender')}的引用→{note}")
    replied = "\n".join(replied_parts) or None
    mns = mentions_note(recs)
    fwd = forward_notes(recs)
    has_image, img = gather_images(recs, reps)
    if not text and fwd:
        text = "（一条合并转发聊天记录，内容见下方展开）"
    if not text and not has_image and not fwd:
        return
    log(f"handle scene={scene} sender={first.get('sender')} mode=chat n={len(recs)} text={text[:40]!r} img={has_image} reply={bool(replied)}{_at_field(mns)}")
    sender = str(first.get("sender") or "")
    uid = int(first.get("user_id") or 0)
    av_note = ""
    img_used = img
    replies, rc = run_qoder(
        scene, sender, uid, text, has_image, replied=replied, img_path=img, mentions=mns, forward_note=fwd,
    )
    reqs = avatar_requests(replies)
    if reqs:  # 模型主动要看头像：拉图拼好带图重问一轮（只重试这一次）
        log(f"avatar req scene={scene} qq={reqs}")
        sheet, label = avatar_sheet(scene, reqs)
        if sheet:
            img_used = sheet
            av_note = f"【头像】你要的头像已附上（{label}），结合头像正常回答刚才的问题；别再输出 [AVATAR] 标记\n"
            replies, rc = run_qoder(
                scene, sender, uid, text, True, replied=replied, img_path=sheet, mentions=mns,
                avatar_note=av_note, forward_note=fwd,
            )
            replies = [r for r in replies if not AVATAR_REQ_RE.search(r)]
        else:
            replies = ["头像这会儿拉不下来，回头再看吧"]
    if not replies and rc == "timeout" and (has_image or av_note):
        # GLM-5.3-Flash 看个别图会卡到超时，CLI 默认模型稳（2026-11-10 毒图29s实证）：换默认模型原题重试一次
        log(f"image round timeout on Flash, retry default model scene={scene}")
        replies, rc = run_qoder(
            scene, sender, uid, text, True, replied=replied, img_path=img_used, mentions=mns,
            avatar_note=av_note, forward_note=fwd, model="",
        )
        replies = [r for r in replies if not AVATAR_REQ_RE.search(r)]
    if not replies:
        log(f"no reply text (rc={rc})")
        if rc == "timeout":  # 看图撞上 Flash 处理不了的图会干等到底，给句话别装死
            try:
                wait_think(received_at)
                send_multi(scene, ["这图我看着费劲，卡住了，缓缓再问"])
            except Exception as e:
                log(f"timeout notice failed: {e}")
        return
    try:
        wait_think(received_at)
        send_multi(scene, replies)
    except Exception as e:
        log(f"send failed: {e}")
        return
    if load_config()["followup"]["enabled"]:
        wait = float(load_config()["followup"].get("wait_sec", 5))
        pending_followups.append({"scene": scene, "due": time.time() + wait, "ts": last_ts})


def _run_search_task(rec: dict, scene: str, sender: str, user_id: int, text: str):
    """路人任务：仅联网搜索（无本地权限），后台跑，与 agent 共用同群串行占位"""
    received_at = rec.get("_recv_at") or 0
    try:
        rep = fetch_reply(rec)
        fwd = forward_notes([rec])
        has_image, img = gather_images([rec], [rep])
        replied = _reply_note(rep)
        mns = mentions_note([rec])
        log(f"handle scene={scene} sender={sender} mode=search text={text[:40]!r} img={has_image} reply={bool(replied)}{_at_field(mns)}")
        replies, rc = run_qoder(scene, sender, user_id, text, has_image, search=True, replied=replied, img_path=img, mentions=mns, forward_note=fwd)
        if not replies:
            log(f"search no reply (rc={rc})")
            return
        wait_think(received_at)
        send_multi(scene, replies)
    except Exception as e:
        log(f"search task error: {e}")
    finally:
        release_agent_slot(scene)


def _dispatch_voice(rec: dict, scene: str, content: str):
    """普通语音回复（不带agent能力）：走聊天同群串行通道；封印中的群不说话"""
    if not content:
        return  # 只喊了"语音"没下文
    if not load_config()["voice"]["enabled"]:
        send_multi(scene, ["语音功能没开"])
        return
    if not scene_chat_ok(scene):
        return
    rec["_voice"] = True
    rec["_voice_text"] = content
    with CHAT_LOCK:
        if scene in chat_busy:
            chat_pending.setdefault(scene, []).append((time.time(), rec))
            log(f"chat busy, voice queued scene={scene}")
            return
        chat_busy.add(scene)
    threading.Thread(
        target=_run_chat_worker,
        args=((time.time(), rec), scene),
        daemon=True,
        name=f"chat-{_scene_safe(scene)}",
    ).start()


CQ_CODE_RE = re.compile(r"\[CQ:[^\]]*\]")
AT_NUM_RE = re.compile(r"@\s*\d{5,}")


def clean_for_tts(text: str) -> str:
    """TTS 前清洗：CQ 码和 @QQ号 会被当成字念出来（2026-11-06 踩过），一律剥掉"""
    text = CQ_CODE_RE.sub("", text)
    text = AT_NUM_RE.sub("", text)
    text = re.sub(r"[，。、,.\s！!？?]{3,}", "，", text)
    return text.strip(" ，。！？")


def _deliver_voice_or_text(scene: str, replies: list):
    """优先语音发送；失败主动说一声并回退文字（聊天语音与 agent 语音汇报共用）"""
    try:
        r = subprocess.run(
            [sys.executable, "-X", "utf8", os.path.join(NB_DIR, "qq_voice.py"),
             "--scene", scene, "--text", clean_for_tts("。".join(replies))[:250]],
            capture_output=True, timeout=420, cwd=NB_DIR,
        )
        tail_out = " | ".join(r.stdout.decode("utf-8", "replace").strip().splitlines()[-2:])
        log(f"voice tool rc={r.returncode}: {tail_out[:160]}")
        if r.returncode == 0:
            return
    except Exception as e:
        log(f"voice tool error: {e}")
    try:
        send_multi(scene, ["语音没发出去，我直接打字了"])
    except Exception as e:
        log(f"fallback notice failed: {e}")
    try:
        send_multi(scene, replies)
    except Exception as e:
        log(f"send failed: {e}")


def _chat_reply_voice(scene: str, rec: dict, received_at: float = 0):
    """语音回复：AI 答复合成小风音色 silk 发群；合成/发送失败主动说一声并回退文字"""
    content = str(rec.get("_voice_text") or "")
    rep = fetch_reply(rec) if rec.get("reply_id") else None
    replied = _reply_note(rep)
    mns = mentions_note([rec])
    fwd = forward_notes([rec])
    has_image, img = gather_images([rec], [rep])
    log(f"handle scene={scene} sender={rec.get('sender')} mode=voice text={content[:40]!r} img={has_image}{_at_field(mns)}")
    replies, rc = run_qoder(
        scene, str(rec.get("sender") or ""), int(rec.get("user_id") or 0),
        content, has_image, replied=replied, img_path=img, voice=True, mentions=mns, forward_note=fwd,
    )
    if not replies:
        log(f"no reply text (rc={rc})")
        return
    wait_think(received_at or rec.get("_recv_at") or 0)
    _deliver_voice_or_text(scene, replies)


def handle(rec: dict):
    rec["_recv_at"] = time.time()
    scene = str(rec.get("scene"))
    sender = str(rec.get("sender"))
    user_id = int(rec.get("user_id") or 0)
    text = (rec.get("text") or "").strip()
    if try_recall_command(rec, scene, user_id):
        return  # 撤回指令（回复bot消息说"撤回"）：不走AI
    if try_feedback_command(rec, scene, user_id, text):
        return  # 反馈指令：原样记档不走AI
    if try_help_command(rec, scene, user_id, text):
        return  # 帮助指令：功能清单不走AI
    if try_todo_command(rec, scene, user_id, text):
        return  # 记账本：每人一本，纯代码不走AI（封印群也可用）
    if try_forward_command(rec, scene, user_id, text):
        return  # 主人的定向转发/发群指令
    canon_text = _canon_la(text)
    vm = re.match(r"^语音[！!，,、\s]*(.*)$", canon_text, re.S)
    if vm is not None and _is_to_me(rec):  # 裸"语音！内容"：不带agent，直接语音回复
        _dispatch_voice(rec, scene, vm.group(1).strip())
        return
    is_agent = False
    is_owner = user_id == OWNER_ID
    matched_pfx = next((orig for canon, orig in CANON_PREFIXES if _canon_la(text).startswith(canon)), None)
    if matched_pfx:
        task = text[len(matched_pfx):].lstrip(" ！!，,、").strip()
        if not task:
            return  # 只喊咒语没下文，当口癖处理
        if try_op_command(scene, user_id, task):
            return
        if try_seal_command(scene, user_id, matched_pfx, task):
            return
        if not _is_to_me(rec):
            return  # 只是提到咒语头（非命令、非@bot）：不当任务，也不调侃
        if task in ("随机表情",):  # 内置表情指令贴写时（@+咒语+关键词）会同时命中 meme 通道，桥侧跳过避免 agent 重复执行
            return
        if matched_pfx.startswith("古娜拉黑暗之神"):  # 带咒语音：主人=agent能力+语音汇报；其他人=普通语音
            voice_content = _parse_voice(task)
            if voice_content is not None:
                if not voice_content:
                    return  # 只喊了"语音"没下文
                if not load_config()["voice"]["enabled"]:
                    send_multi(scene, ["语音功能没开"])
                    return
                if is_owner:
                    rec["_voice"] = True
                    rec["_voice_text"] = voice_content
                    if not acquire_agent_slot(scene):
                        with AGENT_LOCK:
                            cur_kind = agent_busy.get(scene)
                        if cur_kind == "search":
                            pending_agent.setdefault(scene, []).append((rec, scene, sender, user_id, voice_content))
                            log(f"search running, owner voice-task queued scene={scene}")
                        else:
                            _inbox_append(scene, f"{sender}({user_id})", voice_content)
                            log(f"agent busy, voice interjection queued scene={scene}")
                        return
                    _inbox_clear(scene)
                    threading.Thread(
                        target=_run_agent_task,
                        args=(rec, scene, sender, user_id, voice_content),
                        daemon=True,
                        name=f"agent-{_scene_safe(scene)}",
                    ).start()
                    return
                _dispatch_voice(rec, scene, voice_content)  # 非主人：不带agent能力，普通语音聊天
                return
        if not is_owner:  # 路人念咒：降级为仅联网搜索（无任何本地权限），开关可关
            if not load_config()["search"]["enabled"]:
                log(f"agent denied scene={scene} user={user_id} text={task[:40]!r}")
                try:
                    send_multi(scene, ["咒语念得挺熟练", "但这招只听我家主人的😌"])
                except Exception as e:
                    log(f"deny send failed: {e}")
                return
            if not acquire_agent_slot(scene, "search"):
                send_multi(scene, ["手头还有活没干完，稍等再喊"])
                return
            threading.Thread(
                target=_run_search_task,
                args=(rec, scene, sender, user_id, task),
                daemon=True,
                name=f"search-{_scene_safe(scene)}",
            ).start()
            return
        text = task
        is_agent = True
    if try_screenshot_command(rec, scene, user_id, _canon_la(text)):
        return  # @bot 截屏：截宿主机屏幕发图，不走AI
    if is_agent:  # agent 任务挪后台线程跑（同群串行），期间插话进收件箱由任务自己吸收
        if not acquire_agent_slot(scene):
            with AGENT_LOCK:
                cur_kind = agent_busy.get(scene)
            if cur_kind == "search":
                pending_agent.setdefault(scene, []).append((rec, scene, sender, user_id, text))
                log(f"search running, owner task queued scene={scene} text={text[:40]!r}")
            else:
                _inbox_append(scene, f"{sender}({user_id})", text)
                log(f"agent busy, interjection queued scene={scene} text={text[:40]!r}")
            return
        _inbox_clear(scene)
        threading.Thread(
            target=_run_agent_task,
            args=(rec, scene, sender, user_id, text),
            daemon=True,
            name=f"agent-{_scene_safe(scene)}",
        ).start()
        return
    if not scene_chat_ok(scene):
        return  # 群默认封印：AI 聊天静默（主人的咒语/agent 任务/撤回/截屏不受影响），解禁咒开通
    if not (text or rec.get("images") or rec.get("reply_id") or _has_forward(rec)):
        return  # 空消息（无字无图无引用）不接；合并转发卡片在 _chat_reply 里展开
    with CHAT_LOCK:  # 聊天回复后台跑：同群处理期间新来的@攒队列，下一轮合并成一次回复
        if scene in chat_busy:
            chat_pending.setdefault(scene, []).append((time.time(), rec))
            log(f"chat busy, queued scene={scene} pending={len(chat_pending[scene])} text={text[:30]!r}")
            return
        chat_busy.add(scene)
    threading.Thread(
        target=_run_chat_worker,
        args=((time.time(), rec), scene),
        daemon=True,
        name=f"chat-{_scene_safe(scene)}",
    ).start()


def run_consider(scene: str, reason: str):
    """让 AI 看一眼这个群要不要说话（#6 追看 / #8 群活跃插话共用）；说了才发，不想说就沉默"""
    if agent_busy_scene(scene) or chat_busy_scene(scene):
        return  # 该群有 agent 任务或回复在跑，别抢话
    last_consider[scene] = time.time()
    now = time.time()
    recent = [r for ts, r in list(scenes.get(scene) or []) if now - ts < 600][-12:]  # 例行插话也要能看图：带上最近10分钟的图
    has_image, img = gather_images(recent, [])
    replies, rc = run_qoder(scene, "", 0, "", has_image, consider=True, img_path=img or BRIDGE_IMG)
    log(f"consider({reason}) scene={scene} img={has_image} -> {'说' + str(len(replies)) + '条' if replies else '沉默'} rc={rc}")
    if not replies:
        return
    try:
        send_multi(scene, replies)
    except Exception as e:
        log(f"consider send failed: {e}")


def check_pending_followups():
    """到点的追看：期间该群有新消息才触发判断，没有就作废（不阻塞轮询主循环）"""
    due = [f for f in pending_followups if time.time() >= f["due"]]
    if not due:
        return
    pending_followups[:] = [f for f in pending_followups if time.time() < f["due"]]
    for f in due:
        new_cnt = sum(1 for ts, r in (scenes.get(f["scene"]) or ()) if ts > f["ts"] and int(r.get("user_id") or 0) != SELF_ID)
        if new_cnt:
            log(f"followup scene={f['scene']} new={new_cnt}")
            run_consider(f["scene"], "followup")


def _answer_avatar_req(req: dict, ans_path: str):
    # 直接用 CLI 默认模型：GLM-5.3-Flash 看个别图会卡死到超时，默认模型稳（2026-11-10 毒图29s实证）；上限240s，qq_avatar 那头最多等300s
    desc = vision_describe(req.get("path", ""), req.get("ask", ""), timeout=240, model="")
    try:
        with open(ans_path, "w", encoding="utf-8") as f:
            json.dump({"req_ts": req.get("ts"), "desc": desc}, f, ensure_ascii=False)
    except OSError as e:
        log(f"avatar answer write failed: {e}")


def check_avatar_requests():
    """qq_avatar.py 的看图请求文件（agent 肚子里嵌套起 CLI 会挂，改成桥代跑看图调用，答案写回文件）；
    删请求文件=认领，防止重复处理"""
    import glob

    for req_path in glob.glob(os.path.join(NB_DIR, "tmp", "avatar_req_*.json")):
        try:
            with open(req_path, encoding="utf-8") as f:
                req = json.load(f)
            os.remove(req_path)
        except (OSError, ValueError):
            continue
        qq = req.get("qq")
        if not qq:
            continue
        log(f"avatar describe (bridge) qq={qq}")
        threading.Thread(target=_answer_avatar_req, args=(req, os.path.join(NB_DIR, "tmp", f"avatar_ans_{qq}.json")),
                         daemon=True, name=f"avatar-{qq}").start()


def maybe_passive():
    """群活跃插话（#8）：窗口内消息数达到阈值才让 AI 看看要不要说话，按群冷却"""
    cfg = load_config()["passive"]
    if not cfg.get("enabled"):
        return
    window = float(cfg.get("window_min", 10)) * 60
    threshold = int(cfg.get("threshold", 30))
    gap = float(cfg.get("min_gap_min", 10)) * 60
    now = time.time()
    for scene, dq in list(scenes.items()):
        if str(scene).startswith("private:"):
            continue
        if not scene_chat_ok(scene):
            continue
        g = ((load_config().get("groups") or {}).get(str(scene)) or {}).get("passive") or {}
        if g.get("enabled") is False:
            continue
        cnt = sum(1 for ts, r in dq if ts > now - window and int(r.get("user_id") or 0) != SELF_ID)
        if cnt < threshold:
            continue
        if now - last_consider.get(scene, 0) < gap:
            continue
        log(f"passive hit scene={scene} count={cnt}/{threshold}")
        run_consider(scene, "passive")


def connect():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(VM_HOST, 22, VM_USER, VM_PWD, timeout=10)
    return c, c.open_sftp()


def main():
    global offset, partial
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        log(f"default config written: {CONFIG_FILE}")
    last_passive = 0.0
    log(f"host bridge starting (vm={VM_HOST})")
    client, sftp = None, None
    while True:
        try:
            if client is None or not client.get_transport().is_active():
                client, sftp = connect()
                try:
                    offset = sftp.stat(REMOTE_LOG).st_size
                except IOError:
                    offset = 0
                partial = b""
                log(f"connected, start at offset {offset}")
            try:
                size = sftp.stat(REMOTE_LOG).st_size
            except IOError:
                time.sleep(2)
                continue
            if size < offset:
                log("log rotated, reset offset")
                offset, partial = 0, b""
            if size > offset:
                with sftp.open(REMOTE_LOG, "rb") as f:
                    f.seek(offset)
                    chunk = f.read(size - offset)
                offset += len(chunk)
                data = partial + chunk
                lines = data.split(b"\n")
                partial = lines.pop()
                for raw in lines:
                    if not raw.strip():
                        continue
                    try:
                        rec = json.loads(raw.decode("utf-8", "replace"))
                    except Exception:
                        continue
                    if rec.get("scene"):
                        scene_log(str(rec["scene"])).append((time.time(), rec))
                    if not (_is_to_me(rec) or _is_spell_text(rec) or _is_recall_cmd(rec)) or int(rec.get("user_id") or 0) == SELF_ID:
                        continue
                    if rec.get("meme"):
                        continue
                    handle(rec)
            check_pending_followups()
            check_avatar_requests()
            if time.time() - last_passive >= 60:
                last_passive = time.time()
                maybe_passive()
            time.sleep(2)
        except KeyboardInterrupt:
            log("stopped")
            return
        except Exception as e:
            log(f"loop error: {e}")
            try:
                if client:
                    client.close()
            except Exception:
                pass
            client, sftp = None, None
            time.sleep(5)


if __name__ == "__main__":
    main()
