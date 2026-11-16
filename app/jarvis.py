# -*- coding: utf-8 -*-
"""
jarvis.py — ジャービス（PC版）v2 メインアプリ（単一ファイル）。

鳥さんの Windows パソコンで動く、完全オフラインのAI執事。
v2 の形:
  - 常駐するのは丸い球（ジャービスの顔）だけ。大きい窓は最初は出ない。
  - 声で話しかけるのが基本。ずっと聞き耳を立てていて、話し終わると自動で返事する。
    ボタンを押す必要はない（マイクボタンは騒がしい部屋用の予備）。
  - 声で聞き耳を止めることもできる（「静かにして」で静かモード、「聞いて」で再開）。
  - エージェントモードでは本物のPCも操作する（クリック・キー・入力など）。
  - 黒いコンソール窓は絶対に出さない。全ての子プロセスは run_hidden() 経由。
  - 読み上げは sanitize_for_speech() で掃除してから。変な記号や行動行は読み上げない。

通信は 127.0.0.1 のみ。外部ネットワークには一切出ない。聞き耳も端末の中だけ。
"""

import ctypes
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

# Windows 専用モジュールは失敗しても import で落ちないようにする
_winsound = None
if sys.platform == "win32":
    try:
        import winsound as _winsound_mod
        _winsound = _winsound_mod
    except Exception:
        _winsound = None

# 音声入力（Vosk + sounddevice）は任意。無くてもアプリは動く。
_vosk = None
_sd = None
try:
    import vosk as _vosk_mod
    import sounddevice as _sd_mod
    _vosk = _vosk_mod
    _sd = _sd_mod
except Exception:
    _vosk = None
    _sd = None

VOICE_INPUT_AVAILABLE = _vosk is not None and _sd is not None


# ===========================================================================
# 純粋ロジック（GUIにも音にも触らない。単体テスト対象）
# ===========================================================================

# ---------------------------------------------------------------------------
# 思考タグ除去
# ---------------------------------------------------------------------------

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"<think>.*$", re.DOTALL | re.IGNORECASE)


def strip_think(text):
    """<think>...</think> を取り除く。閉じタグが無い場合も末尾まで除去。"""
    if not text:
        return ""
    text = _THINK_RE.sub("", text)
    text = _THINK_OPEN_RE.sub("", text)
    text = text.replace("</s>", "")
    return text.strip()


# ---------------------------------------------------------------------------
# 買い物ガード（ナビと同じルール: 買い物・支払いだけ確認、それ以外は自動）
# ---------------------------------------------------------------------------

SHOPPING_KEYWORDS_JA = ["購入", "注文", "支払", "決済", "買う", "カート", "チェックアウト", "レジ"]
SHOPPING_KEYWORDS_EN = ["buy", "pay", "checkout", "order"]


def needs_shopping_confirm(*texts):
    """渡された文字列のどれかに買い物キーワードがあれば True。"""
    for t in texts:
        if not t:
            continue
        for k in SHOPPING_KEYWORDS_JA:
            if k in t:
                return True
        low = t.lower()
        for k in SHOPPING_KEYWORDS_EN:
            if k in low:
                return True
    return False


# ---------------------------------------------------------------------------
# 行動行パーサ（v1 の語彙 + PC操作の新語彙）
# ---------------------------------------------------------------------------

_ACTION_PREFIXES = [
    ("ファイル作成", "create_file"),
    ("ファイル編集", "edit_file"),
    ("名前をクリック", "click_name"),
    ("見てクリック", "see_click"),
    ("見て", "see"),
    ("ダブルクリック", "double_click"),
    ("右クリック", "right_click"),
    ("クリック", "click"),
    ("スクロール", "scroll"),
    ("ドラッグ", "drag"),
    ("おわり", "done"),
    ("開く", "open"),
    ("アプリ", "app"),
    ("コマンド", "command"),
    ("丸", "circle"),
    ("線", "line"),
    ("キー", "key"),
    ("入力", "type_text"),
    ("移動", "move"),
    ("前面", "front"),
    ("閉じる", "close_window"),
    ("最大化", "maximize"),
    ("最小化", "minimize"),
    ("明るさ", "brightness"),
    ("スクショ", "screenshot"),
    ("ページ", "page"),
    ("待つ", "wait"),
]

# 読み上げ掃除で「行動行」とみなす接頭辞（おわり は別扱い）
ACTION_LINE_PREFIXES = tuple(p for p, _ in _ACTION_PREFIXES)

_LEADING_JUNK_RE = re.compile(r"^[\s\-・*＊>＞#\d\.、．)）]+")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _clamp_pct(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(100.0, f))


def _numbers(text, count):
    """文字列から数値を count 個取り出す（%記号や空白は無視）。"""
    found = _NUM_RE.findall(text or "")
    vals = [_clamp_pct(x) for x in found[:count]]
    while len(vals) < count:
        vals.append(0.0)
    return vals


def _first_number(text, default=0.0):
    found = _NUM_RE.findall(text or "")
    if not found:
        return default
    try:
        return float(found[0])
    except ValueError:
        return default


def _split_action_line(line):
    """'ファイル作成: 名前' / 'ファイル作成： 名前' を (種類, 残り) に分ける。"""
    s = _LEADING_JUNK_RE.sub("", line.strip())
    for prefix, kind in _ACTION_PREFIXES:
        if s.startswith(prefix):
            rest = s[len(prefix):]
            rest = rest.lstrip()
            if rest[:1] in (":", "："):
                rest = rest[1:]
            return kind, rest.strip()
    return None, s


def parse_action(text):
    """
    モデルの返事テキストを解釈して dict を返す。

    v1 の返り値に加えて:
      {"type": "click"/"double_click"/"right_click", "x": .., "y": ..}
      {"type": "key", "spec": "ctrl+c"}        ← key_spec_to_vk() で VK に変換
      {"type": "type_text", "text": ...}
      {"type": "scroll", "amount": -3}          ← 正=下、負=上（目安）
      {"type": "wait", "seconds": 2.0}          ← 0〜10 に丸める
      {"type": "text", "text": ...}             ← 行動行が無い=普通の返事
    """
    cleaned = strip_think(text or "")
    if not cleaned:
        return {"type": "text", "text": ""}

    lines = cleaned.splitlines()
    for i, line in enumerate(lines):
        kind, rest = _split_action_line(line)
        if kind is None:
            continue
        body = "\n".join(lines[i + 1:]).strip("\n")
        if kind in ("create_file", "edit_file"):
            name = rest.split()[0] if rest.split() else "無題.txt"
            return {"type": kind, "name": name, "body": body}
        if kind == "open":
            return {"type": "open", "name": rest}
        if kind == "app":
            return {"type": "app", "target": rest}
        if kind == "command":
            cmd = rest if rest else body
            return {"type": "command", "cmd": cmd.strip()}
        if kind == "circle":
            x, y = _numbers(rest, 2)
            return {"type": "circle", "x": x, "y": y}
        if kind == "line":
            x1, y1, x2, y2 = _numbers(rest, 4)
            return {"type": "line", "x1": x1, "y1": y1, "x2": x2, "y2": y2}
        if kind in ("click", "double_click", "right_click"):
            x, y = _numbers(rest, 2)
            return {"type": kind, "x": x, "y": y}
        if kind == "key":
            return {"type": "key", "spec": rest.replace(" ", "").lower()}
        if kind == "type_text":
            txt = rest if rest else body
            return {"type": "type_text", "text": txt.strip()}
        if kind == "scroll":
            amount = int(_first_number(rest, 0))
            amount = max(-1200, min(1200, amount))
            return {"type": "scroll", "amount": amount}
        if kind == "wait":
            sec = _first_number(rest, 0.0)
            sec = max(0.0, min(10.0, sec))
            return {"type": "wait", "seconds": sec}
        if kind == "move":
            x, y = _numbers(rest, 2)
            return {"type": "move", "x": x, "y": y}
        if kind == "drag":
            x1, y1, x2, y2 = _numbers(rest, 4)
            return {"type": "drag", "x1": x1, "y1": y1, "x2": x2, "y2": y2}
        if kind in ("front", "close_window", "maximize", "minimize"):
            return {"type": kind, "title": rest}
        if kind == "click_name":
            return {"type": "click_name", "name": rest}
        if kind == "brightness":
            level = int(_first_number(rest, 50))
            return {"type": "brightness", "level": max(0, min(100, level))}
        if kind == "screenshot":
            return {"type": "screenshot", "name": rest if rest else "スクショ.png"}
        if kind == "see":
            return {"type": "see", "question": rest}
        if kind == "see_click":
            return {"type": "see_click", "name": rest}
        if kind == "page":
            return {"type": "page", "question": rest}
        if kind == "done":
            summary = rest if rest else body
            return {"type": "done", "summary": summary.strip()}
    return {"type": "text", "text": cleaned}


# ---------------------------------------------------------------------------
# キー指定 → 仮想キーコード（純粋関数。Windows の VK 値はただの数値）
# ---------------------------------------------------------------------------

_NAMED_VK = {
    "enter": 0x0D, "return": 0x0D,
    "esc": 0x1B, "escape": 0x1B,
    "tab": 0x09,
    "win": 0x5B, "windows": 0x5B,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "backspace": 0x08, "bs": 0x08,
    "delete": 0x2E, "del": 0x2E,
    "space": 0x20,
    "ctrl": 0x11, "control": 0x11,
    "alt": 0x12, "shift": 0x10,
    "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
    "insert": 0x2D,
}
for _i in range(1, 13):
    _NAMED_VK["f%d" % _i] = 0x70 + (_i - 1)


def key_spec_to_vk(spec):
    """
    'ctrl+c' / 'enter' / 'alt+f4' のような指定を VK コードのリストにする。
    押す順番どおり（修飾キーが先）。解釈できない部品があれば None。
    """
    if not spec:
        return None
    parts = [p for p in re.split(r"[+＋]", str(spec).strip().lower()) if p != ""]
    if not parts:
        return None
    vks = []
    for p in parts:
        if p in _NAMED_VK:
            vks.append(_NAMED_VK[p])
        elif len(p) == 1 and ("a" <= p <= "z" or "0" <= p <= "9"):
            vks.append(ord(p.upper()))
        else:
            return None
    return vks


# ---------------------------------------------------------------------------
# Sarashina 形式プロンプト
# ---------------------------------------------------------------------------

def build_sarashina_prompt(system, turns):
    """
    system: システムプロンプト文字列
    turns:  [("user", 文), ("assistant", 文), ...] 最後は user のはず
    戻り値: 生成開始位置までのプロンプト文字列
    """
    parts = []
    if system:
        parts.append("<|system|>" + system.strip() + "</s>")
    for role, text in turns:
        tag = "<|user|>" if role == "user" else "<|assistant|>"
        parts.append(tag + (text or "").strip() + "</s>")
    parts.append("<|assistant|>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# ワークスペース内の安全なパス
# ---------------------------------------------------------------------------

def sanitize_relpath(name):
    """AI が出したファイル名を、ワークスペース内の相対パスとして安全化する。"""
    if not name:
        return "無題.txt"
    n = str(name).strip().strip('"').strip("'")
    n = re.sub(r"^[a-zA-Z]:", "", n)
    n = n.replace("\\", "/")
    parts = []
    for p in n.split("/"):
        p = p.strip()
        if p in ("", ".", ".."):
            continue
        parts.append(p)
    if not parts:
        return "無題.txt"
    return "/".join(parts)


def safe_join(base, name):
    """base の中にあることを保証して絶対パスを返す。外に出る場合は None。"""
    rel = sanitize_relpath(name)
    base_abs = os.path.abspath(base)
    path = os.path.abspath(os.path.join(base_abs, *rel.split("/")))
    if path != base_abs and not path.startswith(base_abs + os.sep):
        return None
    return path


# メディア/音量キー（v2 追加分）
_NAMED_VK.update({
    "volume_up": 0xAF, "volume_down": 0xAE, "mute": 0xAD,
    "next": 0xB0, "prev": 0xB1,
    "play_pause": 0xB3, "play": 0xB3, "pause": 0xB3,
})


# ---------------------------------------------------------------------------
# 破壊的ガード（買い物ガードと同じ確認ダイアログで止める対象）
# ---------------------------------------------------------------------------
# 対象: 電源操作コマンド、ドライブのフォーマット、/s や -Recurse の一括削除。
# README にも同じ一覧を書いてある。ここを変えたら README も変えること。

_DESTRUCTIVE_RES = [
    re.compile(r"\bshutdown\b", re.IGNORECASE),
    re.compile(r"\brestart-computer\b", re.IGNORECASE),
    re.compile(r"\bstop-computer\b", re.IGNORECASE),
    re.compile(r"\blogoff\b", re.IGNORECASE),
    re.compile(r"\bformat\s+[a-z]:", re.IGNORECASE),
    re.compile(r"\b(del|erase)\b[^|&;\n]*/s\b", re.IGNORECASE),
    re.compile(r"\b(rd|rmdir)\b[^|&;\n]*/s\b", re.IGNORECASE),
    re.compile(r"\bremove-item\b[^|&;\n]*-recurse", re.IGNORECASE),
    re.compile(r"\brm\s+-[a-z]*rf\b", re.IGNORECASE),
    re.compile(r"\brm\s+-[a-z]*fr\b", re.IGNORECASE),
]


def needs_destructive_confirm(*texts):
    """電源操作・フォーマット・一括削除らしい文字列なら True。"""
    for t in texts:
        if not t:
            continue
        for rx in _DESTRUCTIVE_RES:
            if rx.search(t):
                return True
    return False


# ---------------------------------------------------------------------------
# 読み上げ掃除（「変なの」を読み上げない）
# ---------------------------------------------------------------------------
# 声に出すのは会話の返事か、エージェントの「おわり」のまとめだけ。
# 行動行・コード・Markdown・URL・パス・絵文字は全部落とす。

_SPEECH_MAX_SENTENCES = 3
_SPEECH_MAX_CHARS = 180

_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_FENCE_OPEN_RE = re.compile(r"```.*$", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_WIN_PATH_RE = re.compile(r"[a-zA-Z]:[\\/][^\s、。，]*")
_UNIX_PATH_RE = re.compile(r"(?<![\w])/(?:[\w.\-]+/)+[\w.\-]*")
_FILE_TOKEN_RE = re.compile(r"[\w\-.]+\.(?:gguf|txt|md|py|json|exe|bat|ps1|zip|png|jpg|jpeg|gif|csv|log|html?|xml|ya?ml|ini|dll)\b", re.IGNORECASE)
_SYMBOL_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF"
    "\U0001F1E6-\U0001F1FF※◎○●◯■□▣▤▥▲△▼▽◆◇♦♠♣♥♦→←↑↓⇒⇔≪≫«»†‡]")
_MD_CHARS_RE = re.compile(r"[#*_>~|]")


def _is_action_line(line):
    s = _LEADING_JUNK_RE.sub("", line.strip())
    return any(s.startswith(p) for p in ACTION_LINE_PREFIXES)


def sanitize_for_speech(text):
    """
    読み上げ専用にテキストを掃除する。返すのは会話として話せる部分だけ。
    何も残らなければ空文字列（= 何も話さない）。
    """
    t = strip_think(text or "")
    if not t:
        return ""
    # コードフェンス（閉じていない場合も含む）
    t = _FENCE_RE.sub(" ", t)
    t = _FENCE_OPEN_RE.sub(" ", t)
    # 行動行を落とす（おわり: の中身だけは会話として残す）
    kept = []
    for line in t.splitlines():
        s = line.strip()
        if not s:
            continue
        kind, rest = _split_action_line(s)
        if kind == "done":
            if rest:
                kept.append(rest)
            continue
        if kind is not None or _is_action_line(s):
            continue
        kept.append(s)
    t = "\n".join(kept)
    # リンクはラベルだけ残す → URL は消す
    t = _LINK_RE.sub(r"\1", t)
    t = _URL_RE.sub(" ", t)
    # パスとファイル名っぽいトークン
    t = _WIN_PATH_RE.sub(" ", t)
    t = _UNIX_PATH_RE.sub(" ", t)
    t = _FILE_TOKEN_RE.sub(" ", t)
    # インラインコード、Markdown記号、絵文字・記号
    t = _INLINE_CODE_RE.sub(" ", t)
    t = _MD_CHARS_RE.sub("", t)
    t = _SYMBOL_RE.sub("", t)
    # 空白を整える
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return ""
    # 文に分けて、先頭から最大3文 / 180文字まで
    sentences = [s for s in re.split(r"(?<=[。！？])", t) if s.strip()]
    out = ""
    for s in sentences:
        if out and (len(out) + len(s) > _SPEECH_MAX_CHARS or
                    out.count("。") + out.count("！") + out.count("？") >= _SPEECH_MAX_SENTENCES):
            break
        if not out and len(s) > _SPEECH_MAX_CHARS:
            out = s[:_SPEECH_MAX_CHARS]
            break
        out += s
        if len(out) >= _SPEECH_MAX_CHARS:
            break
    return out.strip()


# ---------------------------------------------------------------------------
# 聞き耳の状態機械（純粋ロジック寄り。音は扱わず、認識文字列で遷移する）
# ---------------------------------------------------------------------------
# 状態:
#   LISTENING      ずっと聞いている。発話が終わればそのままメッセージになる
#   QUIET          声で止められた状態。再開の言葉だけに反応する
#   FULL_OFF       右クリックメニューで完全に止めた状態。何も聞かない
#   SPEAKING_MUTED 自分の読み上げ中+約1秒。自分の声に反応しないための一時状態
#
# feed() の戻り値: {"action": "message"|"stop_task"|"quiet"|"resume"|"ignore",
#                   "text": 送る本文（message の時）, "reason": ...}

WAKE_PREFIXES = ("ジャービス", "じゃーびす", "じゃあびす")
QUIET_PHRASES = ("聞くのをやめて", "聞き耳を止めて", "静かにして", "ミュート")
RESUME_PHRASES = ("聞き耳を始めて", "聞いてる", "聞いて")
TASK_STOP_PHRASES = ("止めて", "ストップ", "とめて")

_COMPACT_DROP_RE = re.compile(r"[\s、,。．.！!？?・…]+")
_WAKE_STRIP_RE = re.compile(r"^\s*(ジャービス|じゃーびす|じゃあびす)[、,。\s]*")


def _compact(s):
    """照合用に、空白と句読点を全部落とした形にする。"""
    return _COMPACT_DROP_RE.sub("", s or "")


def _strip_wake_prefix(raw):
    """先頭の呼びかけ（ジャービス、）を取り除いた本文を返す。"""
    return _WAKE_STRIP_RE.sub("", raw or "").strip()


def _starts_with_any(compact_text, phrases):
    return any(compact_text == p or compact_text.startswith(p) for p in phrases)


class ListenStateMachine:
    LISTENING = "LISTENING"
    QUIET = "QUIET"
    FULL_OFF = "FULL_OFF"
    SPEAKING_MUTED = "SPEAKING_MUTED"

    ECHO_COOLDOWN_SEC = 1.0

    def __init__(self):
        self.mode = self.LISTENING
        self._speaking = False
        self._cooldown_until = 0.0

    # -- 状態参照 ---------------------------------------------------------
    def state(self, now=None):
        """今の見かけの状態。読み上げ中/直後は SPEAKING_MUTED を返す。"""
        if self.mode == self.LISTENING:
            if self._speaking or self._in_cooldown(now):
                return self.SPEAKING_MUTED
        return self.mode

    def _in_cooldown(self, now):
        if now is None:
            now = time.monotonic()
        return now < self._cooldown_until

    # -- 外部からの操作 -----------------------------------------------------
    def set_speaking(self, speaking, now=None):
        """読み上げの開始/終了を知らせる。終了時は約1秒の余韻ガードを入れる。"""
        if now is None:
            now = time.monotonic()
        if speaking:
            self._speaking = True
        else:
            self._speaking = False
            self._cooldown_until = now + self.ECHO_COOLDOWN_SEC

    def full_stop(self):
        self.mode = self.FULL_OFF

    def full_start(self):
        self.mode = self.LISTENING

    def go_quiet(self):
        if self.mode == self.LISTENING:
            self.mode = self.QUIET

    # -- 認識結果を1つ食わせる ------------------------------------------------
    def feed(self, text, now=None):
        if now is None:
            now = time.monotonic()
        raw = (text or "").strip()
        compact = _compact(raw)

        if self.mode == self.FULL_OFF:
            return {"action": "ignore", "reason": "full_off"}
        if not compact:
            return {"action": "ignore", "reason": "empty"}

        if self.mode == self.QUIET:
            body = _compact(_strip_wake_prefix(raw))
            if _starts_with_any(body, RESUME_PHRASES):
                self.mode = self.LISTENING
                return {"action": "resume"}
            return {"action": "ignore", "reason": "quiet"}

        # LISTENING
        if self._speaking or now < self._cooldown_until:
            return {"action": "ignore", "reason": "speaking"}
        body_raw = _strip_wake_prefix(raw)
        body = _compact(body_raw)
        if not body:
            return {"action": "ignore", "reason": "empty"}
        if _starts_with_any(body, QUIET_PHRASES):
            self.mode = self.QUIET
            return {"action": "quiet"}
        if body in TASK_STOP_PHRASES:
            return {"action": "stop_task"}
        if len(body) < 2:
            return {"action": "ignore", "reason": "too_short"}
        return {"action": "message", "text": body_raw.strip()}


# ---------------------------------------------------------------------------
# 設定 (config.json)
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "model_path": "",
    "mmproj_path": "",       # 目のファイル（mmproj GGUF）。任意。入れると画面が見える
    "speaker_id": 11,        # VOICEVOX: 玄野武宏（落ち着いた男性の声）
    "voice_on": True,
    "agent_on": False,
    "listen_on": True,       # ずっと聞き耳（既定オン）
    "autostart": False,      # Windows起動時に自動で出す
    "n_predict": 1024,
    "temperature": 0.7,
}


def load_config(path):
    """設定を読む。無い/壊れている場合は既定値。"""
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update(data)
    except (OSError, ValueError):
        pass
    try:
        cfg["speaker_id"] = int(cfg.get("speaker_id", 11))
    except (TypeError, ValueError):
        cfg["speaker_id"] = 11
    for k in ("voice_on", "agent_on", "listen_on", "autostart"):
        cfg[k] = bool(cfg.get(k, DEFAULT_CONFIG[k]))
    return cfg


def save_config(path, cfg):
    """設定を書く。失敗しても例外を投げない。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False



# ===========================================================================
# パスとリソース（exe 化 / 通常実行の両対応）
# ===========================================================================

FROZEN = bool(getattr(sys, "frozen", False))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def res_path(rel):
    """同梱リソースのパス。exe の中では _MEIPASS、通常はスクリプトのフォルダ。"""
    base = getattr(sys, "_MEIPASS", None) or SCRIPT_DIR
    return os.path.join(base, rel)


def _data_dir():
    """書き込み可能なデータ置き場。exe では %LOCALAPPDATA%\\JarvisPC、通常はスクリプトの隣。"""
    if FROZEN:
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(root, "JarvisPC")
    return SCRIPT_DIR


DATA_DIR = _data_dir()
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
WORKSPACE_DIR = os.path.join(DATA_DIR, "workspace")
MODELS_DIR = os.path.join(DATA_DIR, "models")
SHOTS_DIR = os.path.join(DATA_DIR, "shots")
HELPERS_DIR = os.path.join(DATA_DIR, "helpers")
LLAMA_DIR = os.path.join(DATA_DIR, "llama") if FROZEN else os.path.join(SCRIPT_DIR, "llama")
LLAMA_EXE = os.path.join(LLAMA_DIR, "llama-server.exe")
VOSK_MODEL_NAME = "vosk-model-small-ja-0.22"
VOSK_MODEL_DIR = (os.path.join(DATA_DIR, VOSK_MODEL_NAME) if FROZEN
                  else os.path.join(SCRIPT_DIR, VOSK_MODEL_NAME))
SERVER_BASE = "http://127.0.0.1:8080"


def ensure_data_dirs():
    for d in (DATA_DIR, WORKSPACE_DIR, MODELS_DIR, SHOTS_DIR, HELPERS_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass


def prepare_bundled_resources():
    """
    exe の初回起動時、同梱の llama/ と Vosk モデルをデータフォルダへコピーする
    （exe の一時展開先からは長時間プロセスを動かさないため）。通常実行では何もしない。
    """
    if not FROZEN:
        return
    ensure_data_dirs()
    for name, dest in (("llama", LLAMA_DIR), (VOSK_MODEL_NAME, VOSK_MODEL_DIR)):
        try:
            src = res_path(name)
            if os.path.isdir(src) and not os.path.isdir(dest):
                shutil.copytree(src, dest)
        except Exception:
            pass


# ===========================================================================
# 子プロセスは全部ここを通す（黒いコンソール窓を絶対に出さない）
# ===========================================================================

CREATE_NO_WINDOW = 0x08000000


def _hidden_startupinfo():
    if sys.platform != "win32":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return si


def hidden_popen(args, hide_window=True, **kw):
    """
    窓を出さずにプロセスを起動する。
    hide_window=False の時は CREATE_NO_WINDOW だけにし、起動先の GUI を隠さない
    （ユーザーが頼んだアプリを開く時用）。
    """
    if sys.platform == "win32":
        kw.setdefault("creationflags", CREATE_NO_WINDOW)
        if hide_window:
            kw.setdefault("startupinfo", _hidden_startupinfo())
    return subprocess.Popen(args, **kw)


def run_hidden(args, hide_window=True, **kw):
    """subprocess.run の窓なし版。完了まで待つ。"""
    if sys.platform == "win32":
        kw.setdefault("creationflags", CREATE_NO_WINDOW)
        if hide_window:
            kw.setdefault("startupinfo", _hidden_startupinfo())
    return subprocess.run(args, **kw)


def _decode(b):
    """コマンド出力を文字列に。UTF-8→cp932 の順に試す。"""
    if not b:
        return ""
    for enc in ("utf-8", "cp932"):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode("utf-8", "replace")


def open_path(path):
    """OS の関連付けで開く。"""
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - ユーザー/AI指定のローカルファイルを開く正規の方法
    else:
        hidden_popen(["xdg-open", path], hide_window=False)


# ===========================================================================
# 読み上げ（VOICEVOX → Windows標準音声。どちらも窓は出ない）
# ===========================================================================

VOICEVOX_BASE = "http://127.0.0.1:50021"
DEFAULT_SPEAKER = 11  # 玄野武宏


def _http(url, data=None, timeout=10):
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def detect_voicevox(timeout=1.5):
    """VOICEVOX が起動していればバージョン文字列、無ければ None。"""
    try:
        req = urllib.request.Request(VOICEVOX_BASE + "/version")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace").strip().strip('"')
    except Exception:
        return None


def _split_sentences(text, max_len=120):
    """長い文を句読点で区切って読みやすくする。"""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if not text:
        return []
    chunks, buf = [], ""
    for part in re.split(r"(?<=[。！？!?\n])", text):
        if not part:
            continue
        if len(buf) + len(part) > max_len and buf:
            chunks.append(buf)
            buf = part
        else:
            buf += part
    if buf:
        chunks.append(buf)
    out = []
    for c in chunks:
        while len(c) > max_len:
            out.append(c[:max_len])
            c = c[max_len:]
        if c:
            out.append(c)
    return out


class VoiceEngine:
    """読み上げエンジン。speak_async() は別スレッドで動き、UI を止めない。"""

    def __init__(self, speaker_id=DEFAULT_SPEAKER):
        self.speaker_id = speaker_id
        self._stop = threading.Event()
        self._thread = None
        self._sapi_proc = None
        self._lock = threading.Lock()

    def speak_async(self, text, on_done=None):
        self.stop()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._speak_all, args=(text, on_done), daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if _winsound is not None:
            try:
                _winsound.PlaySound(None, _winsound.SND_PURGE)
            except Exception:
                pass
        with self._lock:
            proc, self._sapi_proc = self._sapi_proc, None
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass

    def is_speaking(self):
        return self._thread is not None and self._thread.is_alive()

    def _speak_all(self, text, on_done):
        ok = False
        try:
            for chunk in _split_sentences(text):
                if self._stop.is_set():
                    break
                if self._speak_voicevox(chunk):
                    ok = True
                    continue
                if self._stop.is_set():
                    break
                if self._speak_sapi(chunk):
                    ok = True
        except Exception:
            ok = False
        if on_done is not None:
            try:
                on_done(ok)
            except Exception:
                pass

    def _speak_voicevox(self, text):
        try:
            q = urllib.parse.urlencode({"text": text, "speaker": int(self.speaker_id)})
            raw = _http(VOICEVOX_BASE + "/audio_query?" + q, data=b"", timeout=15)
            query = json.loads(raw.decode("utf-8"))
            body = json.dumps(query).encode("utf-8")
            q2 = urllib.parse.urlencode({"speaker": int(self.speaker_id)})
            req = urllib.request.Request(
                VOICEVOX_BASE + "/synthesis?" + q2, data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=60) as r:
                wav = r.read()
            if not wav or _winsound is None:
                return False
            import tempfile
            fd, path = tempfile.mkstemp(suffix=".wav", prefix="jarvis_")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(wav)
                _winsound.PlaySound(path, _winsound.SND_FILENAME)
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
            return True
        except Exception:
            return False

    def _speak_sapi(self, text):
        if sys.platform != "win32":
            return False
        safe = text.replace("'", "''").replace("\r", " ").replace("\n", " ")
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "try { $s.SelectVoice('Microsoft Haruka Desktop') } catch { "
            "  try { $s.SelectVoice('Microsoft Haruka') } catch {} }; "
            "$s.Speak('" + safe + "')"
        )
        try:
            with self._lock:
                # -WindowStyle Hidden + CREATE_NO_WINDOW + SW_HIDE の三重で窓を出さない
                self._sapi_proc = hidden_popen(
                    ["powershell", "-NoProfile", "-NonInteractive",
                     "-WindowStyle", "Hidden", "-Command", script],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                proc = self._sapi_proc
            proc.wait(timeout=120)
            return True
        except Exception:
            return False


# ===========================================================================
# 聞き耳（Vosk でずっと聞く。端末の中だけ。録音も送信もしない）
# ===========================================================================

class VoiceListener:
    """
    常時聞き耳スレッド。
    認識の確定結果だけを ListenStateMachine に食わせ、出てきた action を
    on_event(kind, payload) でアプリへ知らせる（スレッドから直接呼ばれる）。
      kind: "message"（payload=本文）/ "stop_task" / "quiet" / "resume"
    QUIET の間は認識器の文法を再開フレーズだけに絞る。
    マイクボタン用の「次の1回だけ拾う」もここで受ける（QUIET の回避用）。
    """

    def __init__(self, model_dir, machine, on_event):
        self.model_dir = model_dir
        self.machine = machine
        self.on_event = on_event
        self._thread = None
        self._stop = threading.Event()
        self._oneshot = threading.Event()
        self.available = VOICE_INPUT_AVAILABLE and os.path.isdir(model_dir)
        self.last_error = ""

    # -- 公開API ---------------------------------------------------------
    def start(self):
        if not self.available:
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def request_oneshot(self):
        """次の確定発話を、QUIET でも1回だけメッセージとして拾う。"""
        self._oneshot.set()

    # -- 内部 ---------------------------------------------------------------
    def _emit(self, kind, payload=None):
        try:
            self.on_event(kind, payload)
        except Exception:
            pass

    def _make_recognizer(self, model, quiet):
        rec = _vosk.KaldiRecognizer(model, 16000)
        if quiet:
            grammar = json.dumps(list(RESUME_PHRASES) + ["[unk]"], ensure_ascii=False)
            try:
                rec.SetGrammar(grammar)
            except Exception:
                pass
        return rec

    def _run(self):
        try:
            _vosk.SetLogLevel(-1)
            model = _vosk.Model(self.model_dir)
        except Exception as e:
            self.last_error = str(e)
            self.available = False
            return
        quiet_now = self.machine.mode == ListenStateMachine.QUIET
        rec = self._make_recognizer(model, quiet_now)
        try:
            stream = _sd.RawInputStream(samplerate=16000, blocksize=8000,
                                        dtype="int16", channels=1)
            stream.start()
        except Exception as e:
            self.last_error = str(e)
            self.available = False
            return
        try:
            while not self._stop.is_set():
                try:
                    data, _overflow = stream.read(4000)
                except Exception:
                    time.sleep(0.05)
                    continue
                # QUIET と通常で認識器を切り替える
                want_quiet = self.machine.mode == ListenStateMachine.QUIET
                if want_quiet != quiet_now:
                    quiet_now = want_quiet
                    rec = self._make_recognizer(model, quiet_now)
                if not rec.AcceptWaveform(bytes(data)):
                    continue  # 確定していない部分結果は使わない
                try:
                    result = json.loads(rec.Result())
                except ValueError:
                    continue
                text = (result.get("text") or "").strip()
                if not text:
                    continue
                if self._oneshot.is_set():
                    self._oneshot.clear()
                    if self.machine.mode == ListenStateMachine.QUIET:
                        self.machine.full_start()
                    self._emit("message", _strip_wake_prefix(text) or text)
                    continue
                act = self.machine.feed(text)
                kind = act.get("action")
                if kind == "message":
                    self._emit("message", act.get("text", ""))
                elif kind in ("stop_task", "quiet", "resume"):
                    self._emit(kind)
        except Exception as e:
            self.last_error = str(e)
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass



# ---------------------------------------------------------------------------
# 視覚（Vision）の純粋部分: 座標パースとメッセージ組み立て
# ---------------------------------------------------------------------------

def parse_vision_coords(text):
    """
    VLモデルの返事から、0〜1000 物差しの中心座標を取り出して % に直す。
    見つからなければ None。
    """
    cleaned = strip_think(text or "")
    found = _NUM_RE.findall(cleaned)
    if len(found) < 2:
        return None
    try:
        x = float(found[0]) / 10.0
        y = float(found[1]) / 10.0
    except ValueError:
        return None
    return (max(0.0, min(100.0, x)), max(0.0, min(100.0, y)))


def build_vision_messages(question, image_b64, mime="image/jpeg"):
    """OpenAI 互換の画像つきメッセージを作る（llama-server がモデル自身の形式を当てる）。"""
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": question},
            {"type": "image_url",
             "image_url": {"url": "data:%s;base64,%s" % (mime, image_b64)}},
        ],
    }]


# ---------------------------------------------------------------------------
# ページ理解の純粋部分: ブラウザ判定、本文掃除、ヘルパー出力パース
# ---------------------------------------------------------------------------

BROWSER_CLASS_NAMES = {"Chrome_WidgetWin_1", "Chrome_WidgetWin_0", "MozillaWindowClass"}
BROWSER_TITLE_HINTS = ("Google Chrome", "Microsoft Edge", "Mozilla Firefox",
                       "Brave", "Chromium", "Opera", "Vivaldi")


def is_browser_window(title, class_name):
    """ウィンドウの題名とクラス名からブラウザらしいかを推定する（目安）。"""
    if class_name and class_name.strip() in BROWSER_CLASS_NAMES:
        return True
    t = title or ""
    return any(h in t for h in BROWSER_TITLE_HINTS)


def cleanup_page_text(text, max_chars=6000):
    """ページ本文を行単位で重複除去し、空行を落とし、最大文字数で切る。"""
    seen = set()
    lines = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        lines.append(s)
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars]
    return out


def parse_page_helper_output(stdout):
    """UIA ヘルパーの標準出力から JSON を取り出して整える。失敗は None。"""
    if not stdout:
        return None
    start = stdout.find("{")
    end = stdout.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(stdout[start:end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return {
        "title": str(data.get("title", "")),
        "url": str(data.get("url", "")),
        "text": cleanup_page_text(str(data.get("text", ""))),
        "is_browser": bool(data.get("is_browser", False)),
    }


def build_page_question(question, title, url, text):
    """ページの文章だけから答えさせるための、地に足のついた質問文を作る。"""
    return (
        "今開いているウェブページについて、鳥さんの質問に答えてください。\n"
        "ページの題名: %s\n"
        "アドレス: %s\n"
        "ページの文章（ここにあることだけから答えること。文章が足りない時は、その旨をはっきり言うこと）:\n"
        "----\n%s\n----\n"
        "質問: %s" % (title or "（不明）", url or "（不明）", text or "", question)
    )


# ===========================================================================
# ジャービスの顔（光るドットの球）+ 聞き耳インジケーター
# ===========================================================================

import math

GLOBE_BG = "#0a1420"
GLOBE_DISC = "#0d2036"
GLOBE_DISC_EDGE = "#1d4e5f"
GLOBE_DOT_FAR = "#14586b"
GLOBE_DOT_MID = "#2ec4d8"
GLOBE_DOT_NEAR = "#e8ffff"
GLOBE_GLOW = "#39d5e8"
TRANSPARENT_KEY = "#ff00ff"  # 窓の透過色（球と吹き出しで共用）


def _precompute_dots():
    dots = []
    for i in range(11):
        lat_deg = -75 + i * 15
        lat = math.radians(lat_deg)
        n = max(4, int(round(25 * math.cos(lat))))
        offset = (i % 2) * (math.pi / n)
        for j in range(n):
            lon = offset + j * (2 * math.pi / n)
            dots.append((lat, lon))
    return dots


class GlobeCanvas(tk.Canvas):
    """緯度リング状ドット（約190個）の球。聞き耳の状態も小さな点で示す。"""

    def __init__(self, master, size=240, bg=GLOBE_BG, **kw):
        kw.setdefault("bg", bg)
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("bd", 0)
        super().__init__(master, width=size, height=size, **kw)
        self.size = size
        self.dots = _precompute_dots()
        self._angle = 0.0
        self._t = 0.0
        self._active = False
        self._pulse_start = None
        self._running = False
        self._listen_mode = ListenStateMachine.LISTENING

    def start(self):
        if not self._running:
            self._running = True
            self._tick()

    def stop(self):
        self._running = False

    def set_active(self, active):
        self._active = bool(active)

    def pulse(self):
        self._pulse_start = self._t

    def set_listen_mode(self, mode):
        """聞き耳の状態表示: LISTENING=明るい点 / QUIET=暗く+橙の点 / FULL_OFF=ほぼ消灯。"""
        self._listen_mode = mode

    def _tick(self):
        if not self._running:
            return
        try:
            if not self.winfo_exists():
                self._running = False
                return
        except tk.TclError:
            self._running = False
            return
        self._t += 0.04
        self._angle += 0.030 if self._active else 0.010
        self._draw()
        self.after(40, self._tick)

    def _draw(self):
        s = self.size
        cx = cy = s / 2.0
        base_r = s * 0.36
        dim = 1.0
        if self._listen_mode == ListenStateMachine.QUIET:
            dim = 0.55
        elif self._listen_mode == ListenStateMachine.FULL_OFF:
            dim = 0.3
        breath = 1.0 + 0.02 * math.sin(self._t * 1.4)
        r = base_r * breath

        self.delete("globe")
        self.create_oval(cx - r * 1.12, cy - r * 1.12, cx + r * 1.12, cy + r * 1.12,
                         fill=GLOBE_DISC, outline=GLOBE_DISC_EDGE, width=1, tags="globe")
        glow_r = r * (0.45 + 0.05 * math.sin(self._t * 2.0))
        self.create_oval(cx - glow_r, cy - glow_r, cx + glow_r, cy + glow_r,
                         fill="#123a4d", outline="", tags="globe")

        amp = 0.055 if self._active else 0.018
        pts = []
        for lat, lon in self.dots:
            rot = lon + self._angle
            wave = 1.0 + amp * math.sin(lat * 4.0 - self._t * 5.0 + lon * 2.0)
            x = cx + r * wave * math.cos(lat) * math.sin(rot)
            y = cy - r * math.sin(lat)
            z = math.cos(lat) * math.cos(rot)
            pts.append((z, x, y))
        pts.sort(key=lambda p: p[0])

        def shade(hex_color, f):
            h = hex_color.lstrip("#")
            rr, gg, bb = (int(h[i:i + 2], 16) for i in (0, 2, 4))
            return "#%02x%02x%02x" % (int(rr * f), int(gg * f), int(bb * f))

        for z, x, y in pts:
            if z < -0.15:
                color, rad = GLOBE_DOT_FAR, 1.4
            elif z < 0.45:
                color, rad = GLOBE_DOT_MID, 2.0
            else:
                color, rad = GLOBE_DOT_NEAR, 2.7
            if dim < 1.0:
                color = shade(color, dim)
            self.create_oval(x - rad, y - rad, x + rad, y + rad,
                             fill=color, outline="", tags="globe")

        if self._pulse_start is not None:
            dt = self._t - self._pulse_start
            if dt < 1.0:
                pr = r * (0.3 + 1.1 * dt)
                self.create_oval(cx - pr, cy - pr, cx + pr, cy + pr,
                                 outline=GLOBE_GLOW, width=2, tags="globe")
            else:
                self._pulse_start = None

        # 聞き耳インジケーター（下端の小さな点）
        iy = cy + r * 1.12 + max(4, s * 0.03)
        if self._listen_mode == ListenStateMachine.LISTENING:
            blink = 0.75 + 0.25 * math.sin(self._t * 2.4)
            col = shade("#39e88f", blink)
            self.create_oval(cx - 4, iy - 4, cx + 4, iy + 4, fill=col, outline="", tags="globe")
        elif self._listen_mode == ListenStateMachine.QUIET:
            self.create_oval(cx - 4, iy - 4, cx + 4, iy + 4, fill="#e8a039", outline="", tags="globe")
        elif self._listen_mode == ListenStateMachine.SPEAKING_MUTED:
            self.create_oval(cx - 4, iy - 4, cx + 4, iy + 4, fill="#39d5e8", outline="", tags="globe")


# ===========================================================================
# 本物の画面への丸・線オーバーレイ（短命・表示専用・窓は隠し属性つき）
# ===========================================================================

OVERLAY_TRANSPARENT = "black"
GLOW_COLORS = ["#0e5a6b", "#1b93a8", "#39d5e8", "#bffbff"]


class _Overlay:
    def __init__(self, master, draw_fn, seconds=4.0):
        self.win = tk.Toplevel(master)
        self.win.overrideredirect(True)
        try:
            self.win.attributes("-topmost", True)
        except tk.TclError:
            pass
        w = self.win.winfo_screenwidth()
        h = self.win.winfo_screenheight()
        self.w, self.h = w, h
        self.win.geometry("%dx%d+0+0" % (w, h))
        self.canvas = tk.Canvas(self.win, width=w, height=h, bg=OVERLAY_TRANSPARENT,
                                highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        try:
            self.win.attributes("-transparentcolor", OVERLAY_TRANSPARENT)
        except tk.TclError:
            pass
        self._draw_fn = draw_fn
        self._phase = 0
        self._steps = max(1, int(seconds / 0.08))
        self._animate()

    def _animate(self):
        if self._phase > self._steps:
            try:
                self.win.destroy()
            except tk.TclError:
                pass
            return
        self.canvas.delete("all")
        pulse = 1.0 + 0.06 * (1 if self._phase % 2 == 0 else -1)
        self._draw_fn(self.canvas, self.w, self.h, pulse)
        self._phase += 1
        self.win.after(80, self._animate)


def _glow_circle(canvas, cx, cy, r):
    for i, col in enumerate(GLOW_COLORS):
        rr = r + (len(GLOW_COLORS) - i) * 3
        canvas.create_oval(cx - rr, cy - rr, cx + rr, cy + rr,
                           outline=col, width=2 + i)


def _glow_line(canvas, x1, y1, x2, y2):
    for i, col in enumerate(GLOW_COLORS):
        canvas.create_line(x1, y1, x2, y2, fill=col, width=2 + i * 2)


def show_circle(master, x_pct, y_pct, seconds=4.0):
    def draw(canvas, w, h, pulse):
        _glow_circle(canvas, w * x_pct / 100.0, h * y_pct / 100.0, 46 * pulse)
    try:
        _Overlay(master, draw, seconds)
        return True
    except Exception:
        return False


def show_line(master, x1_pct, y1_pct, x2_pct, y2_pct, seconds=4.0):
    def draw(canvas, w, h, pulse):
        _glow_line(canvas, w * x1_pct / 100.0, h * y1_pct / 100.0,
                   w * x2_pct / 100.0, h * y2_pct / 100.0)
    try:
        _Overlay(master, draw, seconds)
        return True
    except Exception:
        return False



# ===========================================================================
# 本物のPC操作（ctypes のみ。pyautogui は使わない）
# ===========================================================================

_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_WHEEL = 0x0800
_KEYEVENTF_KEYUP = 0x0002
_WHEEL_DELTA = 120
_SW_RESTORE = 9
_SW_MAXIMIZE = 3
_SW_MINIMIZE = 6
_WM_CLOSE = 0x0010
_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002


def _user32():
    if sys.platform != "win32":
        return None
    try:
        return ctypes.windll.user32
    except Exception:
        return None


def _kernel32():
    if sys.platform != "win32":
        return None
    try:
        return ctypes.windll.kernel32
    except Exception:
        return None


def screen_size():
    u = _user32()
    if u is None:
        return (1920, 1080)
    return (u.GetSystemMetrics(0), u.GetSystemMetrics(1))


def _pct_to_px(x_pct, y_pct):
    w, h = screen_size()
    return (int(w * x_pct / 100.0), int(h * y_pct / 100.0))


def pc_move(x_pct, y_pct):
    u = _user32()
    if u is None:
        return False
    try:
        x, y = _pct_to_px(x_pct, y_pct)
        return bool(u.SetCursorPos(x, y))
    except Exception:
        return False


def pc_click(x_pct, y_pct, button="left", double=False):
    u = _user32()
    if u is None:
        return False
    try:
        if not pc_move(x_pct, y_pct):
            return False
        time.sleep(0.05)
        down, up = ((_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP) if button == "right"
                    else (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP))
        u.mouse_event(down, 0, 0, 0, None)
        u.mouse_event(up, 0, 0, 0, None)
        if double:
            time.sleep(0.08)
            u.mouse_event(down, 0, 0, 0, None)
            u.mouse_event(up, 0, 0, 0, None)
        return True
    except Exception:
        return False


def pc_drag(x1_pct, y1_pct, x2_pct, y2_pct, steps=24, duration=0.45):
    u = _user32()
    if u is None:
        return False
    try:
        if not pc_move(x1_pct, y1_pct):
            return False
        time.sleep(0.06)
        u.mouse_event(_MOUSEEVENTF_LEFTDOWN, 0, 0, 0, None)
        sx, sy = _pct_to_px(x1_pct, y1_pct)
        ex, ey = _pct_to_px(x2_pct, y2_pct)
        for i in range(1, steps + 1):
            x = int(sx + (ex - sx) * i / steps)
            y = int(sy + (ey - sy) * i / steps)
            u.SetCursorPos(x, y)
            time.sleep(max(0.005, duration / steps))
        u.mouse_event(_MOUSEEVENTF_LEFTUP, 0, 0, 0, None)
        return True
    except Exception:
        return False


def pc_scroll(amount):
    """amount: 正=下、負=上。|amount|<=20 は「クリック数」として 120 倍する。"""
    u = _user32()
    if u is None:
        return False
    try:
        delta = amount * _WHEEL_DELTA if -20 <= amount <= 20 else amount
        u.mouse_event(_MOUSEEVENTF_WHEEL, 0, 0, ctypes.c_ulong(delta & 0xFFFFFFFF), None)
        return True
    except Exception:
        return False


def pc_key(vk_list):
    """VK のリストを、修飾キーの順に押して逆順に離す。"""
    u = _user32()
    if u is None or not vk_list:
        return False
    try:
        for vk in vk_list:
            u.keybd_event(vk, 0, 0, None)
            time.sleep(0.03)
        time.sleep(0.05)
        for vk in reversed(vk_list):
            u.keybd_event(vk, 0, _KEYEVENTF_KEYUP, None)
            time.sleep(0.02)
        return True
    except Exception:
        return False


def set_clipboard_text(text):
    u, k = _user32(), _kernel32()
    if u is None or k is None:
        return False
    try:
        data = (text or "").encode("utf-16-le") + b"\x00\x00"
        if not u.OpenClipboard(None):
            return False
        try:
            u.EmptyClipboard()
            h = k.GlobalAlloc(_GMEM_MOVEABLE, len(data))
            if not h:
                return False
            ptr = k.GlobalLock(h)
            if not ptr:
                return False
            ctypes.memmove(ptr, data, len(data))
            k.GlobalUnlock(h)
            return bool(u.SetClipboardData(_CF_UNICODETEXT, h))
        finally:
            u.CloseClipboard()
    except Exception:
        return False


def get_clipboard_text():
    u, k = _user32(), _kernel32()
    if u is None or k is None:
        return ""
    try:
        if not u.OpenClipboard(None):
            return ""
        try:
            h = u.GetClipboardData(_CF_UNICODETEXT)
            if not h:
                return ""
            ptr = k.GlobalLock(h)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)
            finally:
                k.GlobalUnlock(h)
        finally:
            u.CloseClipboard()
    except Exception:
        return ""


def pc_type_text(text):
    """今フォーカスされている場所へ、クリップボード経由で貼り付けて入力する。"""
    if not set_clipboard_text(text or ""):
        return False
    time.sleep(0.08)
    return pc_key([0x11, ord("V")])  # Ctrl+V


# ---------------------------------------------------------------------------
# ウィンドウ列挙・操作
# ---------------------------------------------------------------------------

def _window_text(u, hwnd):
    n = u.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value.strip()


def _window_class(u, hwnd):
    buf = ctypes.create_unicode_buffer(256)
    try:
        u.GetClassNameW(hwnd, buf, 256)
        return buf.value.strip()
    except Exception:
        return ""


def list_windows():
    """見えているウィンドウの (hwnd, 題名, クラス名) 一覧。Windows 以外は空。"""
    out = []
    u = _user32()
    if u is None:
        return out
    try:
        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def cb(hwnd, _):
            try:
                if u.IsWindowVisible(hwnd):
                    t = _window_text(u, hwnd)
                    if t:
                        out.append((hwnd, t, _window_class(u, hwnd)))
            except Exception:
                pass
            return True
        u.EnumWindows(cb, 0)
    except Exception:
        return []
    return out


def list_window_titles():
    return [t for _h, t, _c in list_windows()][:30]


def foreground_window():
    """(hwnd, 題名, クラス名)。取れなければ (None, "", "")。"""
    u = _user32()
    if u is None:
        return (None, "", "")
    try:
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return (None, "", "")
        return (hwnd, _window_text(u, hwnd), _window_class(u, hwnd))
    except Exception:
        return (None, "", "")


def find_window_by_title(subtitle):
    """題名に部分一致する最初のウィンドウ (hwnd, 題名)。無ければ (None, "")。"""
    sub = (subtitle or "").lower()
    if not sub:
        return (None, "")
    for hwnd, title, _cls in list_windows():
        if sub in title.lower():
            return (hwnd, title)
    return (None, "")


def window_action(subtitle, action):
    """
    action: "front" / "close" / "maximize" / "minimize"
    戻り値: (成功か, 対象の題名 or 理由)
    """
    u = _user32()
    if u is None:
        return (False, "この環境ではウィンドウ操作ができません")
    hwnd, title = find_window_by_title(subtitle)
    if not hwnd:
        return (False, "『%s』を含むウィンドウが見つかりませんでした" % subtitle)
    try:
        if action == "front":
            if u.IsIconic(hwnd):
                u.ShowWindow(hwnd, _SW_RESTORE)
            u.SetForegroundWindow(hwnd)
        elif action == "close":
            u.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
        elif action == "maximize":
            u.ShowWindow(hwnd, _SW_MAXIMIZE)
        elif action == "minimize":
            u.ShowWindow(hwnd, _SW_MINIMIZE)
        else:
            return (False, "分からない操作です")
        return (True, title)
    except Exception as e:
        return (False, str(e))


# ---------------------------------------------------------------------------
# UI Automation ヘルパー（PowerShell、同梱文字列をデータフォルダへ書いて使う）
#   モード click: 表示名で要素を探してクリック
#   モード page : 前面ブラウザの題名・アドレス・本文を JSON で返す
# ---------------------------------------------------------------------------

UIA_HELPER_PS1 = r"""
param([string]$Mode = "click", [string]$Name = "")
$ErrorActionPreference = "Stop"
try {
  Add-Type -AssemblyName UIAutomationClient
  Add-Type -AssemblyName UIAutomationTypes
  Add-Type @"
using System;
using System.Runtime.InteropServices;
public class U32 {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr hWnd, System.Text.StringBuilder lpClassName, int nMaxCount);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, IntPtr e);
}
"@
  $fg = [U32]::GetForegroundWindow()

  if ($Mode -eq "page") {
    $sb = New-Object System.Text.StringBuilder 256
    [void][U32]::GetClassName($fg, $sb, 256)
    $cls = $sb.ToString()
    $root = [System.Windows.Automation.AutomationElement]::FromHandle($fg)
    $title = $root.Current.Name
    $isBrowser = ($cls -like "Chrome_WidgetWin*" -or $cls -eq "MozillaWindowClass")
    if (-not $isBrowser) {
      @{ is_browser = $false; title = $title; url = ""; text = "" } | ConvertTo-Json -Compress
      exit 0
    }
    $url = ""
    try {
      $editCond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Edit)
      $edits = $root.FindAll([System.Windows.Automation.TreeScope]::Subtree, $editCond)
      foreach ($e in $edits) {
        try {
          $vp = $e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
          $v = $vp.Current.Value
          if ($v -match "^(https?://|www\.)" -or $v -match "\.[a-z]{2,}(/|$)") { $url = $v; break }
        } catch {}
      }
    } catch {}
    $text = ""
    try {
      $docCond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Document)
      $doc = $root.FindFirst([System.Windows.Automation.TreeScope]::Subtree, $docCond)
      if ($doc) {
        $all = $doc.FindAll([System.Windows.Automation.TreeScope]::Subtree,
                            [System.Windows.Automation.Condition]::TrueCondition)
        $names = New-Object System.Collections.Generic.List[string]
        $total = 0; $count = 0
        foreach ($el in $all) {
          try {
            $n = $el.Current.Name
            if ($n) { $names.Add($n); $total += $n.Length; $count += 1 }
          } catch {}
          if ($total -gt 30000 -or $count -gt 4000) { break }
        }
        $text = [string]::Join("`n", $names)
      }
    } catch {}
    @{ is_browser = $true; title = $title; url = $url; text = $text } | ConvertTo-Json -Compress
    exit 0
  }

  # click モード
  $roots = @()
  if ($fg -ne [IntPtr]::Zero) { $roots += [System.Windows.Automation.AutomationElement]::FromHandle($fg) }
  $roots += [System.Windows.Automation.AutomationElement]::RootElement
  $cond = [System.Windows.Automation.Condition]::TrueCondition
  $found = $null
  foreach ($rootEl in $roots) {
    if ($null -eq $rootEl) { continue }
    $els = $rootEl.FindAll([System.Windows.Automation.TreeScope]::Subtree, $cond)
    foreach ($el in $els) {
      try {
        $n = $el.Current.Name
        if ($n -and $n.Contains($Name)) { $found = $el; break }
      } catch {}
    }
    if ($found) { break }
  }
  if (-not $found) { Write-Output "NG 見つかりませんでした"; exit 0 }
  $fname = $found.Current.Name
  $done = $false
  try {
    $pat = $found.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
    $pat.Invoke(); $done = $true
  } catch {}
  if (-not $done) {
    try {
      if (-not $found.Current.IsOffscreen) {
        $r = $found.Current.BoundingRectangle
        if ($r.Width -gt 0 -and $r.Height -gt 0 -and $r.Width -lt 100000) {
          $cx = [int]($r.X + $r.Width / 2); $cy = [int]($r.Y + $r.Height / 2)
          [void][U32]::SetCursorPos($cx, $cy)
          Start-Sleep -Milliseconds 60
          [U32]::mouse_event(0x0002, 0, 0, 0, [IntPtr]::Zero)
          [U32]::mouse_event(0x0004, 0, 0, 0, [IntPtr]::Zero)
          $done = $true
        }
      }
    } catch {}
  }
  if ($done) { Write-Output ("OK " + $fname) } else { Write-Output "NG 押せませんでした" }
} catch {
  Write-Output ("NG " + $_.Exception.Message)
}
"""


def _ensure_uia_helper():
    """ヘルパー .ps1 をデータフォルダへ書き出してパスを返す。"""
    ensure_data_dirs()
    path = os.path.join(HELPERS_DIR, "uia_helper.ps1")
    try:
        need = True
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                need = f.read() != UIA_HELPER_PS1
        if need:
            with open(path, "w", encoding="utf-8") as f:
                f.write(UIA_HELPER_PS1)
    except OSError:
        return None
    return path


def _run_uia_helper(mode, name="", timeout=40):
    """ヘルパーを窓なしで実行し、標準出力を返す。失敗は空文字列。"""
    if sys.platform != "win32":
        return ""
    path = _ensure_uia_helper()
    if not path:
        return ""
    try:
        cp = run_hidden(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-ExecutionPolicy", "Bypass", "-File", path,
             "-Mode", mode, "-Name", name or ""],
            capture_output=True, timeout=timeout)
        return _decode(cp.stdout).strip()
    except Exception:
        return ""


def uia_click_by_name(name):
    """表示名で要素をクリック。(成功か, 見つけた名前 or 理由)"""
    out = _run_uia_helper("click", name)
    if out.startswith("OK "):
        return (True, out[3:].strip())
    if out.startswith("NG "):
        return (False, out[3:].strip())
    return (False, "うまく動きませんでした")


def uia_read_page():
    """前面ブラウザのページ情報を dict で返す。取れなければ None。"""
    out = _run_uia_helper("page", "", timeout=60)
    return parse_page_helper_output(out)


def set_brightness(level):
    """ノートPCの明るさを WMI で設定する。(成功か, メッセージ)"""
    if sys.platform != "win32":
        return (False, "この環境では明るさを変えられません")
    level = max(0, min(100, int(level)))
    script = (
        "try { $m = Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods "
        "-ErrorAction Stop; if ($m) { $m.WmiSetBrightness(1, %d); exit 0 } else { exit 1 } } "
        "catch { exit 1 }" % level)
    try:
        cp = run_hidden(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-Command", script],
            capture_output=True, timeout=30)
        if cp.returncode == 0:
            return (True, "明るさを %d%% にしました" % level)
    except Exception:
        pass
    return (False, "このパソコンでは明るさを変えられませんでした（対応していない機種があります）")


# ---------------------------------------------------------------------------
# ページ読み取り（UIA → クリップボード の順に試す）
# ---------------------------------------------------------------------------

def read_page_via_clipboard():
    """Ctrl+A → Ctrl+C でページ本文を取り、クリップボードを元に戻す。"""
    saved = get_clipboard_text()
    try:
        if not pc_key(key_spec_to_vk("ctrl+a")):
            return ""
        time.sleep(0.15)
        if not pc_key(key_spec_to_vk("ctrl+c")):
            return ""
        time.sleep(0.45)
        return cleanup_page_text(get_clipboard_text())
    finally:
        if saved:
            set_clipboard_text(saved)


def get_page_context():
    """
    前面ページの読み取り結果を返す。
    {ok, title, url, text, route} / {ok: False, reason, title}
    """
    _hwnd, title, cls = foreground_window()
    if not is_browser_window(title, cls):
        return {"ok": False, "reason": "not_browser", "title": title}
    page = uia_read_page()
    text = ""
    url = ""
    ptitle = title
    if page:
        text = page.get("text", "")
        url = page.get("url", "")
        ptitle = page.get("title") or title
    route = "uia"
    if len(text) < 200:
        cb = read_page_via_clipboard()
        if len(cb) > len(text):
            text = cb
            route = "clipboard"
    if not text:
        return {"ok": False, "reason": "no_text", "title": ptitle}
    return {"ok": True, "title": ptitle, "url": url, "text": text, "route": route}


# ---------------------------------------------------------------------------
# 画面キャプチャ
# ---------------------------------------------------------------------------

def capture_screen_image():
    """PIL の Image を返す。Pillow が無い等では None。"""
    try:
        from PIL import ImageGrab
        return ImageGrab.grab()
    except Exception:
        return None


def capture_screen_to(path):
    img = capture_screen_image()
    if img is None:
        return False
    try:
        img.save(path)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Windows 起動時の自動起動（レジストリ HKCU Run）
# ---------------------------------------------------------------------------

def _run_key(open_mode):
    import winreg
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                          r"Software\Microsoft\Windows\CurrentVersion\Run",
                          0, open_mode)


def set_autostart(enabled):
    if sys.platform != "win32":
        return False
    try:
        import winreg
        key = _run_key(winreg.KEY_SET_VALUE)
        try:
            if enabled:
                if FROZEN:
                    cmd = '"%s"' % sys.executable
                else:
                    cmd = '"%s" "%s"' % (sys.executable, os.path.abspath(__file__))
                winreg.SetValueEx(key, "JarvisPC", 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, "JarvisPC")
                except FileNotFoundError:
                    pass
        finally:
            winreg.CloseKey(key)
        return True
    except Exception:
        return False


def autostart_enabled():
    if sys.platform != "win32":
        return False
    try:
        import winreg
        key = _run_key(winreg.KEY_QUERY_VALUE)
        try:
            winreg.QueryValueEx(key, "JarvisPC")
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False



# ===========================================================================
# llama-server 管理 + 生成 + 視覚
# ===========================================================================

PERSONA = (
    "あなたは「ジャービス」という名前のAI執事です。鳥さんのWindowsパソコンの中で、"
    "完全オフラインで動いています。明るく、やさしく、丁寧な日本語で答えてください。"
    "わかりやすく答えるのが得意です。わからないことは正直に「わかりません」と言ってください。"
)

AGENT_SYSTEM_EXTRA = (
    "今はエージェントモードです。頼みごとを進めるため、毎回「行動を1行だけ」で答えてください。"
    "説明や挨拶は書かないでください。使える行動は毎回の状態メッセージに書いてあります。"
)

AGENT_FORMAT_HINT = (
    "次の行動を1つだけ、次の形式のどれか1行で答えてください（説明は書かない）:\n"
    "ファイル作成: ファイル名（次の行から本文）\n"
    "ファイル編集: ファイル名（次の行から新しい本文）\n"
    "開く: ファイル名\n"
    "アプリ: アプリ名またはファイルのパス\n"
    "コマンド: 実行するコマンド\n"
    "クリック: x% y%（本物の画面。0〜100の割合）\n"
    "ダブルクリック: x% y% / 右クリック: x% y% / 移動: x% y%\n"
    "ドラッグ: x1% y1% x2% y2%\n"
    "キー: キー名（例 enter、ctrl+c、alt+f4、volume_up、play_pause）\n"
    "入力: 今フォーカスされている場所へ打ち込む文字\n"
    "スクロール: 3（下）や -3（上）のような数\n"
    "名前をクリック: 画面に見えているボタンの名前\n"
    "見て: 画面について聞きたいこと（目が設定されている時だけ）\n"
    "見てクリック: 押したい物の名前（目が設定されている時だけ）\n"
    "ページ: 開いているページへの質問\n"
    "前面: ウィンドウの題名の一部 / 閉じる: 題名の一部 / 最大化: 題名の一部 / 最小化: 題名の一部\n"
    "丸: x% y%（画面に丸を描く）\n"
    "線: x1% y1% x2% y2%\n"
    "明るさ: 0〜100\n"
    "スクショ: 保存するファイル名\n"
    "待つ: 秒数（最大10）\n"
    "おわり: 終わったことのまとめ"
)

TEXT_EXTS = {".txt", ".md", ".py", ".json", ".csv", ".bat", ".ps1", ".html",
             ".htm", ".xml", ".ini", ".log", ".yaml", ".yml"}
IMAGE_EXTS = {".png", ".gif", ".jpg", ".jpeg", ".bmp"}


class LlamaServer:
    def __init__(self):
        self.proc = None
        self.started_by_us = False
        self.mmproj_used = False

    @staticmethod
    def healthy(timeout=1.5):
        try:
            req = urllib.request.Request(SERVER_BASE + "/health")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status == 200
        except Exception:
            return False

    def start(self, model_path, mmproj_path, on_status):
        """サーバーを起動し、準備完了まで見守る（別スレッドで呼ぶこと）。窓は出ない。"""
        if self.healthy():
            on_status("ready", "llama-server は既に動いています")
            return True
        if not os.path.isfile(LLAMA_EXE):
            on_status("error", "llama-server.exe が見つかりません（同梱ファイルが壊れているかもしれません）")
            return False
        if not model_path or not os.path.isfile(model_path):
            on_status("error", "モデル（GGUFファイル）が選ばれていません")
            return False
        args = [LLAMA_EXE, "-m", model_path,
                "--host", "127.0.0.1", "--port", "8080",
                "-c", "4096", "--threads", "4"]
        self.mmproj_used = False
        if mmproj_path and os.path.isfile(mmproj_path):
            args += ["--mmproj", mmproj_path]
            self.mmproj_used = True
        try:
            log_path = os.path.join(DATA_DIR, "llama-server.log")
            logf = open(log_path, "ab")
            self.proc = hidden_popen(args, stdout=logf, stderr=logf)
            self.started_by_us = True
        except Exception as e:
            on_status("error", "llama-server を起動できませんでした: %s" % e)
            return False
        on_status("loading", "モデルを読み込んでいます…（大きいモデルは1〜2分かかります）")
        for _ in range(240):
            if self.proc.poll() is not None:
                on_status("error", "llama-server が途中で終了しました。llama-server.log を見てください")
                return False
            if self.healthy():
                on_status("ready", "準備ができました")
                return True
            time.sleep(1.0)
        on_status("error", "サーバーの準備が時間内に終わりませんでした")
        return False

    def stop(self):
        if self.proc is not None and self.started_by_us:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None
        self.started_by_us = False
        self.mmproj_used = False


class LlamaClient:
    """文章は Sarashina 形式 /completion（ダメなら chat API）。視覚は chat API 専用。"""

    def __init__(self, cfg):
        self.cfg = cfg
        # True の時は /v1/chat/completions でモデル自身のテンプレートに任せる
        # （Vision モデル用）。文章専用 Sarashina の時だけ False（手組み形式）。
        self.chat_api_mode = False

    def generate(self, system, turns, on_token, stop_event):
        if self.chat_api_mode:
            return self._chat_completions(system, turns, on_token, stop_event)
        prompt = build_sarashina_prompt(system, turns)
        try:
            return self._completion(prompt, on_token, stop_event)
        except urllib.error.HTTPError as e:
            if e.code not in (404, 501):
                raise
            return self._chat_completions(system, turns, on_token, stop_event)

    def _completion(self, prompt, on_token, stop_event):
        payload = {
            "prompt": prompt,
            "n_predict": int(self.cfg.get("n_predict", 1024)),
            "temperature": float(self.cfg.get("temperature", 0.7)),
            "stop": ["</s>"],
            "stream": True,
            "cache_prompt": True,
        }
        req = urllib.request.Request(
            SERVER_BASE + "/completion",
            data=json.dumps(payload).encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")
        got_any = False
        with urllib.request.urlopen(req, timeout=900) as r:
            for raw in r:
                if stop_event.is_set():
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    data = json.loads(line[5:])
                except ValueError:
                    continue
                content = data.get("content", "")
                if content:
                    got_any = True
                    on_token(content)
                if data.get("stop"):
                    break
        return got_any

    def _chat_completions(self, system, turns, on_token, stop_event):
        messages = [{"role": "system", "content": system}]
        for role, text in turns:
            messages.append({"role": role, "content": text})
        payload = {
            "messages": messages,
            "max_tokens": int(self.cfg.get("n_predict", 1024)),
            "temperature": float(self.cfg.get("temperature", 0.7)),
        }
        req = urllib.request.Request(
            SERVER_BASE + "/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=900) as r:
            data = json.loads(r.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"]
        if stop_event.is_set():
            return False
        on_token(text)
        return True

    def vision_ask(self, question, image_b64, mime="image/jpeg", timeout=300):
        """
        画面の画像と質問を VL モデルへ送り、答えの文字列を返す。
        形式はモデル自身のテンプレートに任せる（Sarashina 形式は強制しない）。
        """
        payload = {
            "messages": build_vision_messages(question, image_b64, mime),
            "max_tokens": 512,
            "temperature": 0.2,
        }
        req = urllib.request.Request(
            SERVER_BASE + "/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return strip_think(data["choices"][0]["message"]["content"])


def screenshot_b64(max_width=1024):
    """画面を撮って、幅を抑えた JPEG の base64 を返す。失敗は None。"""
    img = capture_screen_image()
    if img is None:
        return None
    try:
        import base64
        import io
        img = img.convert("RGB")
        if img.width > max_width:
            h = int(img.height * max_width / img.width)
            img = img.resize((max_width, h))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None



# ---------------------------------------------------------------------------
# 創作ワークフロー（頼まれた物を、要素どおりに作る）
# ---------------------------------------------------------------------------

CREATION_KEYWORDS = ("作って", "作る", "つくって", "ゲーム", "アプリ", "文書",
                     "ページ", "プログラム", "サイト", "ツール", "スクリプト")


def is_creation_request(text):
    """作り物のお願いらしいかを推定する（エージェントの創作手順の入口）。"""
    t = text or ""
    return any(k in t for k in CREATION_KEYWORDS)


CREATION_SYSTEM_EXTRA = (
    "これは作り物のお願いです。手順を守ってください。"
    "最初に、鳥さんが頼んだ要素を短い「要素リスト」として箇条書きでだけ答えてください（行動行は使わない）。"
    "そのあと、ファイル作成などの行動で、部品ごとに実際に作ってください。"
    "最後の「おわり」では、要素リストと照らし合わせて、入った要素と、できなかった要素を正直に書いてください。"
    "作っていない要素を「ある」と言ってはいけません。"
    "HTMLのゲームやページは、開くだけで動く単一のHTMLファイルにしてください。"
    "ガラス風にしてと言われたら、backdrop-filter のぼかし・半透明・縁の光で Liquid Glass 風に寄せてください。"
)


# ===========================================================================
# メインアプリ（常駐するのは球だけ。会話は吹き出し）
# ===========================================================================

ORB_SIZE = 140
BUBBLE_W = 340
BUBBLE_H = 446
BUBBLE_TAIL = 16
BUBBLE_HIDE_SEC = 12

BRAIN_HINT = (
    "脳みその目安:\n"
    "・おすすめ（日本語も目も1つで）: Sarashina2.2-Vision-3B + 目のファイル(mmproj)\n"
    "・目が要らないなら: Sarashina2.2-3B（文章が一番得意）\n"
    "・速さ重視: Sarashina2.2-1B\n"
    "・目の代わり: Qwen2.5-VL-3B + mmproj\n"
    "ChatGPT 級の賢さそのものは、このパソコンでは動きません（READMEに正直に書いてあります）。"
)


class JarvisApp:
    def __init__(self, root):
        self.root = root
        prepare_bundled_resources()
        ensure_data_dirs()

        self.cfg = load_config(CONFIG_PATH)
        self.machine = ListenStateMachine()
        if not self.cfg.get("listen_on", True):
            self.machine.full_stop()
        self.server = LlamaServer()
        self.client = LlamaClient(self.cfg)
        self.voice = VoiceEngine(self.cfg.get("speaker_id", 11))
        self.listener = VoiceListener(VOSK_MODEL_DIR, self.machine, self._on_voice_event)
        self.voicevox_version = None

        self.ui_q = queue.Queue()
        self.stop_event = threading.Event()
        self.busy = False
        self.history = []
        self._stream_buf = []
        self._started_with = None       # (model_path, mmproj_path)
        self.vision_ready = False

        # Tk変数
        self.model_var = tk.StringVar(value=self.cfg.get("model_path", ""))
        self.mmproj_var = tk.StringVar(value=self.cfg.get("mmproj_path", ""))
        self.speaker_var = tk.StringVar(value=str(self.cfg.get("speaker_id", 11)))
        self.voice_var = tk.BooleanVar(value=bool(self.cfg.get("voice_on", True)))
        self.agent_var = tk.BooleanVar(value=bool(self.cfg.get("agent_on", False)))
        self.listen_var = tk.BooleanVar(value=bool(self.cfg.get("listen_on", True)))
        self.autostart_var = tk.BooleanVar(value=autostart_enabled())
        self.server_status_var = tk.StringVar(value="サーバー: 未起動")
        self.voicevox_status_var = tk.StringVar(value="VOICEVOX: 確認中…")
        self.listen_status_var = tk.StringVar(value="聞き耳: 準備中…")

        self._build_orb()
        self._build_bubble()
        self.desktop_win = None
        self.desktop_frame = None
        self.settings_win = None

        self.root.after(50, self._pump_ui)
        self.root.after(1000, self._bubble_autohide_tick)

        self._post_sys("こんにちは、鳥さん。ジャービスです。球が常駐しています。")
        self._post_sys("話しかけるだけで大丈夫です。ボタンは要りません。")
        if not os.path.isfile(LLAMA_EXE):
            self._post_err("llama-server.exe が見つかりません。同梱ファイルを確認してください。")
        if not self.model_var.get():
            self._post_sys("モデルが未選択です。球を右クリック→「設定」から GGUF を選んでください。")
        else:
            self._start_server_async()
        threading.Thread(target=self._detect_voicevox_thread, daemon=True).start()
        self._init_listener()

    # ------------------------------------------------------------------
    # 設定
    # ------------------------------------------------------------------
    def _save_cfg(self):
        self.cfg["model_path"] = self.model_var.get()
        self.cfg["mmproj_path"] = self.mmproj_var.get()
        self.cfg["voice_on"] = bool(self.voice_var.get())
        self.cfg["agent_on"] = bool(self.agent_var.get())
        self.cfg["listen_on"] = bool(self.listen_var.get())
        try:
            self.cfg["speaker_id"] = int(self.speaker_var.get())
        except ValueError:
            pass
        self.voice.speaker_id = self.cfg.get("speaker_id", 11)
        save_config(CONFIG_PATH, self.cfg)

    # ------------------------------------------------------------------
    # 球（常駐）
    # ------------------------------------------------------------------
    def _build_orb(self):
        root = self.root
        root.overrideredirect(True)
        root.configure(bg=TRANSPARENT_KEY)
        try:
            root.attributes("-topmost", True)
            root.attributes("-transparentcolor", TRANSPARENT_KEY)
        except tk.TclError:
            pass
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        x = max(0, sw - ORB_SIZE - 24)
        y = max(0, sh - ORB_SIZE - 72)
        root.geometry("%dx%d+%d+%d" % (ORB_SIZE, ORB_SIZE, x, y))

        self.globe = GlobeCanvas(root, size=ORB_SIZE, bg=TRANSPARENT_KEY)
        self.globe.pack()
        self.globe.start()

        self._drag = {"x": 0, "y": 0, "wx": 0, "wy": 0, "moved": False}
        self.globe.bind("<ButtonPress-1>", self._orb_press)
        self.globe.bind("<B1-Motion>", self._orb_motion)
        self.globe.bind("<ButtonRelease-1>", self._orb_release)
        self.globe.bind("<Button-3>", self._orb_menu)

    def _orb_press(self, event):
        self._drag = {"x": event.x_root, "y": event.y_root,
                      "wx": self.root.winfo_x(), "wy": self.root.winfo_y(),
                      "moved": False}

    def _orb_motion(self, event):
        dx = event.x_root - self._drag["x"]
        dy = event.y_root - self._drag["y"]
        if abs(dx) + abs(dy) > 4:
            self._drag["moved"] = True
        if self._drag["moved"]:
            nx = self._drag["wx"] + dx
            ny = self._drag["wy"] + dy
            self.root.geometry("+%d+%d" % (nx, ny))
            if self._bubble_visible:
                self._place_bubble()

    def _orb_release(self, event):
        if not self._drag["moved"]:
            if self._bubble_visible:
                self._hide_bubble()
            else:
                self._show_bubble()

    def _orb_menu(self, event):
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="チャットを開く", command=self._show_bubble)
        menu.add_command(label="ジャービスのPC", command=self._open_desktop)
        menu.add_command(label="設定", command=self._open_settings)
        menu.add_separator()
        if self.machine.mode == ListenStateMachine.FULL_OFF:
            menu.add_command(label="聞き耳を始める", command=lambda: self._set_listen(True))
        else:
            menu.add_command(label="聞き耳を止める（完全に）",
                             command=lambda: self._set_listen(False))
        menu.add_checkbutton(label="Windows起動時に自動で出す",
                             variable=self.autostart_var,
                             command=self._on_autostart_toggle)
        menu.add_separator()
        menu.add_command(label="終了", command=self._on_close)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _on_autostart_toggle(self):
        want = bool(self.autostart_var.get())
        if set_autostart(want):
            self.cfg["autostart"] = want
            self._save_cfg()
            self._post_sys("自動起動を%sにしました。" % ("オン" if want else "オフ"))
        else:
            self.autostart_var.set(autostart_enabled())
            self._post_err("自動起動の設定ができませんでした。")

    # ------------------------------------------------------------------
    # 吹き出し（会話UI）
    # ------------------------------------------------------------------
    def _build_bubble(self):
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.configure(bg=TRANSPARENT_KEY)
        try:
            win.attributes("-topmost", True)
            win.attributes("-transparentcolor", TRANSPARENT_KEY)
        except tk.TclError:
            pass
        win.geometry("%dx%d+0+0" % (BUBBLE_W, BUBBLE_H))
        self.bubble = win
        self._bubble_visible = False
        self._bubble_flipped = False
        self._bubble_touch = time.monotonic()

        self.bubble_canvas = tk.Canvas(win, width=BUBBLE_W, height=BUBBLE_H,
                                       bg=TRANSPARENT_KEY, highlightthickness=0, bd=0)
        self.bubble_canvas.place(x=0, y=0)

        self.chat_text = scrolledtext.ScrolledText(
            win, wrap="word", state="disabled", bg="#0d1b2a", fg="#eaf7ff",
            insertbackground="#eaf7ff", font=("Yu Gothic UI", 10),
            relief="flat", padx=6, pady=6)
        for tag, color in (("user", "#9fd0ff"), ("jarvis", "#eaf7ff"),
                           ("log", "#ffb35c"), ("sys", "#7fa8b8"), ("err", "#ff8f8f")):
            self.chat_text.tag_configure(tag, foreground=color)

        self.status_label = tk.Label(win, textvariable=self.listen_status_var,
                                     bg="#10222f", fg="#8fd8e8",
                                     font=("Yu Gothic UI", 8), anchor="w")
        self.input_entry = tk.Entry(win, font=("Yu Gothic UI", 10), bg="#12283d",
                                    fg="#eaf7ff", insertbackground="#eaf7ff", relief="flat")
        self.input_entry.bind("<Return>", lambda e: self._on_send())
        self.input_entry.bind("<KeyPress>", lambda e: self._touch_bubble())
        self.send_button = ttk.Button(win, text="送信", command=self._on_send)
        self.mic_button = ttk.Button(win, text="マイク", command=self._on_mic_button)
        self.agent_check = ttk.Checkbutton(win, text="エージェント", variable=self.agent_var,
                                           command=self._save_cfg)
        self.voice_check = ttk.Checkbutton(win, text="読み上げ", variable=self.voice_var,
                                           command=self._save_cfg)
        self.stop_button = ttk.Button(win, text="止める", command=self._on_stop)
        self.see_button = ttk.Button(win, text="画面を見る", command=self._on_see_screen)
        self.page_button = ttk.Button(win, text="ページを説明", command=self._on_explain_page)
        self.titles_button = ttk.Button(win, text="見せて説明", command=self._on_explain_titles)

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TCheckbutton", background="#10222f", foreground="#d7f3ff",
                        font=("Yu Gothic UI", 9))
        style.configure("TButton", font=("Yu Gothic UI", 9), padding=(5, 2))

        win.bind("<Enter>", lambda e: self._touch_bubble())
        win.withdraw()

    def _draw_bubble_shape(self, flipped, tail_x):
        c = self.bubble_canvas
        c.delete("all")
        body_top = BUBBLE_TAIL if flipped else 0
        body_bottom = BUBBLE_H if flipped else BUBBLE_H - BUBBLE_TAIL
        # しっぽ（本体の後ろに先に描く）
        if flipped:
            c.create_polygon(tail_x - 10, body_top + 2, tail_x + 10, body_top + 2,
                             tail_x, 2, fill="#10222f", outline="#2ec4d8")
        else:
            c.create_polygon(tail_x - 10, body_bottom - 2, tail_x + 10, body_bottom - 2,
                             tail_x, BUBBLE_H - 2, fill="#10222f", outline="#2ec4d8")
        # 角丸の本体（右上→左上→左下→右下 の角を順に結ぶ）
        r = 18
        pts = []
        corners = [(r, body_top + r, range(180, 89, -15)),
                   (BUBBLE_W - r, body_top + r, range(90, -1, -15)),
                   (BUBBLE_W - r, body_bottom - r, range(0, -91, -15)),
                   (r, body_bottom - r, range(270, 179, -15))]
        for cx0, cy0, angs in corners:
            for a in angs:
                ang = math.radians(a)
                pts.append((cx0 + r * math.cos(ang), cy0 - r * math.sin(ang)))
        c.create_polygon(pts, fill="#10222f", outline="#2ec4d8", width=1)

    def _layout_bubble_widgets(self, flipped):
        top = (BUBBLE_TAIL if flipped else 0) + 10
        self.chat_text.place(x=12, y=top, width=BUBBLE_W - 24, height=238)
        self.status_label.place(x=12, y=top + 242, width=BUBBLE_W - 24, height=16)
        self.input_entry.place(x=12, y=top + 264, width=176, height=28)
        self.send_button.place(x=192, y=top + 264, width=52, height=28)
        self.mic_button.place(x=248, y=top + 264, width=80, height=28)
        self.agent_check.place(x=12, y=top + 300, width=104, height=24)
        self.voice_check.place(x=120, y=top + 300, width=88, height=24)
        self.stop_button.place(x=248, y=top + 298, width=80, height=28)
        self.see_button.place(x=12, y=top + 330, width=100, height=28)
        self.page_button.place(x=116, y=top + 330, width=104, height=28)
        self.titles_button.place(x=224, y=top + 330, width=104, height=28)

    def _place_bubble(self):
        ox = self.root.winfo_x()
        oy = self.root.winfo_y()
        ocx = ox + ORB_SIZE // 2
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        bx = max(8, min(sw - BUBBLE_W - 8, ocx - BUBBLE_W // 2))
        by = oy - BUBBLE_H - 10
        flipped = False
        if by < 8:
            flipped = True
            by = oy + ORB_SIZE + 10
            if by + BUBBLE_H > sh - 8:
                by = max(8, sh - BUBBLE_H - 8)
        tail_x = max(28, min(BUBBLE_W - 28, ocx - bx))
        self._bubble_flipped = flipped
        self.bubble.geometry("+%d+%d" % (bx, by))
        self._draw_bubble_shape(flipped, tail_x)
        self._layout_bubble_widgets(flipped)

    def _show_bubble(self):
        self._place_bubble()
        self.bubble.deiconify()
        self._bubble_visible = True
        self._touch_bubble()

    def _hide_bubble(self):
        self.bubble.withdraw()
        self._bubble_visible = False

    def _touch_bubble(self):
        self._bubble_touch = time.monotonic()

    def _bubble_autohide_tick(self):
        try:
            if self._bubble_visible:
                typing = False
                try:
                    typing = (self.bubble.focus_get() == self.input_entry
                              and bool(self.input_entry.get().strip()))
                except Exception:
                    typing = False
                if (not typing and not self.busy
                        and time.monotonic() - self._bubble_touch > BUBBLE_HIDE_SEC):
                    self._hide_bubble()
        except Exception:
            pass
        self.root.after(1000, self._bubble_autohide_tick)

    # ------------------------------------------------------------------
    # UIキュー
    # ------------------------------------------------------------------
    def _pump_ui(self):
        try:
            while True:
                fn = self.ui_q.get_nowait()
                try:
                    fn()
                except Exception:
                    pass
        except queue.Empty:
            pass
        self.root.after(50, self._pump_ui)

    def ui(self, fn):
        self.ui_q.put(fn)

    # ------------------------------------------------------------------
    # チャット表示（吹き出しの中）
    # ------------------------------------------------------------------
    def _append_chat(self, text, tag):
        self.chat_text.configure(state="normal")
        self.chat_text.insert("end", text, tag)
        self.chat_text.see("end")
        self.chat_text.configure(state="disabled")
        self._touch_bubble()

    def _post_user(self, text):
        self.ui(lambda: self._append_chat("鳥さん: %s\n" % text, "user"))

    def _post_jarvis(self, text):
        self.ui(lambda: self._append_chat("ジャービス: %s\n" % text, "jarvis"))

    def _post_log(self, text):
        self.ui(lambda: self._append_chat("▶ %s\n" % text, "log"))

    def _post_sys(self, text):
        self.ui(lambda: self._append_chat("%s\n" % text, "sys"))

    def _post_err(self, text):
        self.ui(lambda: self._append_chat("%s\n" % text, "err"))


    # ------------------------------------------------------------------
    # サーバー操作
    # ------------------------------------------------------------------
    def _refresh_client_mode(self):
        """Vision モデル（または mmproj 使用中）は chat API、文章用 Sarashina は手組み形式。"""
        name = os.path.basename(self.model_var.get()).lower()
        self.client.chat_api_mode = bool(
            self.server.mmproj_used or "vision" in name or "vl" in name)
        self.vision_ready = bool(self.server.mmproj_used)

    def _start_server_async(self):
        if self.server.healthy() and self._started_with == (self.model_var.get(),
                                                            self.mmproj_var.get()):
            self.server_status_var.set("サーバー: 準備OK")
            return
        if self.server.healthy() or self.server.proc is not None:
            self.server.stop()
        self.server_status_var.set("サーバー: 起動中…")
        threading.Thread(target=self._start_server_thread, daemon=True).start()

    def _start_server_thread(self):
        def on_status(kind, msg):
            if kind == "ready":
                self._started_with = (self.model_var.get(), self.mmproj_var.get())
                self._refresh_client_mode()
                self.ui(lambda: self.server_status_var.set("サーバー: 準備OK"))
                self._post_sys("モデルの準備ができました。話しかけてください。")
                if self.vision_ready:
                    self._post_sys("目（mmproj）も入りました。画面を見ることができます。")
            elif kind == "loading":
                self.ui(lambda: self.server_status_var.set("サーバー: 読み込み中…"))
                self._post_sys(msg)
            else:
                self.ui(lambda: self.server_status_var.set("サーバー: エラー"))
                self._post_err(msg)
        self.server.start(self.model_var.get(), self.mmproj_var.get(), on_status)

    def _stop_server(self):
        self.server.stop()
        self._started_with = None
        self.vision_ready = False
        self.server_status_var.set("サーバー: 停止中")
        self._post_sys("サーバーを止めました。")

    def _detect_voicevox_thread(self):
        ver = detect_voicevox()
        self.voicevox_version = ver
        if ver:
            self.ui(lambda: self.voicevox_status_var.set("VOICEVOX: 起動しています（ver %s）" % ver))
        else:
            self.ui(lambda: self.voicevox_status_var.set(
                "VOICEVOX: 見つかりません（Windows標準の音声で読み上げます）"))

    # ------------------------------------------------------------------
    # 聞き耳の管理
    # ------------------------------------------------------------------
    def _init_listener(self):
        if not self.cfg.get("listen_on", True):
            self._sync_listen_ui()
            return
        if not self.listener.available:
            self._post_sys("聞き耳は使えません（音声入力の部品が入っていないか、マイクが使えません）。"
                           "文字入力とマイク以外の機能は使えます。")
            self.listen_status_var.set("聞き耳: 使えません")
            self.globe.set_listen_mode(ListenStateMachine.FULL_OFF)
            return
        self.listener.start()
        self._sync_listen_ui()

    def _sync_listen_ui(self):
        mode = self.machine.mode
        if not self.listener.is_running() and mode != ListenStateMachine.FULL_OFF:
            disp = ListenStateMachine.FULL_OFF
        else:
            disp = mode
        self.globe.set_listen_mode(disp)
        label = {
            ListenStateMachine.LISTENING: "聞き耳: 聞いています（話しかけてください）",
            ListenStateMachine.QUIET: "聞き耳: 静かにしています（「聞いて」で再開）",
            ListenStateMachine.FULL_OFF: "聞き耳: 止まっています",
            ListenStateMachine.SPEAKING_MUTED: "聞き耳: 読み上げ中…",
        }.get(disp, "聞き耳: —")
        self.listen_status_var.set(label)

    def _set_listen(self, on):
        self.listen_var.set(on)
        self.cfg["listen_on"] = on
        self._save_cfg()
        if on:
            self.machine.full_start()
            if self.listener.available:
                self.listener.start()
            else:
                self._post_err("聞き耳の部品が使えないため、始められませんでした。")
        else:
            self.machine.full_stop()
            self.listener.stop()
        self._sync_listen_ui()

    def _on_voice_event(self, kind, payload):
        """聞き耳スレッドからの通知。UI スレッドへ渡す。"""
        if kind == "message":
            self.ui(lambda: self._handle_voice_message(payload or ""))
        elif kind == "stop_task":
            self.ui(self._handle_voice_stop)
        elif kind == "quiet":
            self.ui(self._handle_voice_quiet)
        elif kind == "resume":
            self.ui(self._handle_voice_resume)

    def _handle_voice_message(self, text):
        if not text.strip():
            return
        self.globe.pulse()
        self._show_bubble()
        if self.busy:
            self._post_sys("今、前の仕事をしています。止める時は「止めて」と言ってください。")
            return
        self._dispatch_user_message(text)

    def _handle_voice_stop(self):
        if self.busy:
            self._on_stop()

    def _handle_voice_quiet(self):
        self._sync_listen_ui()
        self._post_sys("静かにします。再開する時は「聞いて」と言ってください。")
        self._speak("分かりました、静かにしています", force=True)

    def _handle_voice_resume(self):
        self._sync_listen_ui()
        self._speak("聞いています", force=True)

    def _on_mic_button(self):
        """予備のマイクボタン。通常の流れでは押す必要はない。"""
        if not self.listener.available:
            self._post_err("音声入力が使えません（部品が入っていないか、マイクがありません）。")
            return
        if self.machine.mode == ListenStateMachine.FULL_OFF:
            self._set_listen(True)
        self.listener.request_oneshot()
        self._post_sys("どうぞ、話してください。（次のひと言を拾います）")
        self.globe.pulse()

    # ------------------------------------------------------------------
    # 読み上げ（掃除してから。自分の声には反応しない）
    # ------------------------------------------------------------------
    def _speak(self, text, force=False):
        if not force and not self.voice_var.get():
            return
        spoken = sanitize_for_speech(text or "")
        if not spoken:
            return
        self.machine.set_speaking(True)
        self.ui(lambda: self.globe.set_active(True))

        def done(ok):
            self.machine.set_speaking(False)
            self.ui(lambda: (self.globe.set_active(False), self._sync_listen_ui()))

        self.voice.speak_async(spoken, on_done=done)

    # ------------------------------------------------------------------
    # 送信まわり
    # ------------------------------------------------------------------
    def _on_stop(self):
        self.stop_event.set()
        self.voice.stop()
        self.machine.set_speaking(False)
        self.globe.set_active(False)
        self._post_sys("止めました。")

    def _on_send(self):
        text = self.input_entry.get().strip()
        if not text:
            return
        self.input_entry.delete(0, "end")
        self._dispatch_user_message(text)

    def _dispatch_user_message(self, text, display_text=None):
        if self.busy:
            self._post_sys("今、前の仕事をしています。終わるまで待つか「止める」を押してください。")
            return
        if not self.server.healthy():
            self._post_err("サーバーが準備できていません。設定からモデルを選ぶか、サーバー起動を押してください。")
            return
        self._show_bubble()
        self._post_user(display_text if display_text is not None else text)
        self.busy = True
        self.stop_event.clear()
        self.globe.pulse()
        self.globe.set_active(True)
        if self.agent_var.get():
            threading.Thread(target=self._agent_loop, args=(text,), daemon=True).start()
        else:
            threading.Thread(target=self._chat_turn, args=(text,), daemon=True).start()

    def _finish_turn(self, reply_text):
        self.busy = False
        self.ui(lambda: self.globe.set_active(False))
        if reply_text and self.voice_var.get():
            self._speak(reply_text)

    # ------------------------------------------------------------------
    # 通常会話
    # ------------------------------------------------------------------
    def _chat_turn(self, user_text):
        self.history.append(("user", user_text))
        turns = self.history[-12:]
        self._stream_buf = []
        self.ui(lambda: self._append_chat("ジャービス: ", "jarvis"))
        ok = False
        try:
            ok = self.client.generate(PERSONA, turns, self._stream_token, self.stop_event)
        except Exception as e:
            self._post_err("うまく答えられませんでした（%s）" % e)
        reply = strip_think("".join(self._stream_buf))
        self.ui(lambda: self._append_chat("\n", "jarvis"))
        if ok and reply:
            self.history.append(("assistant", reply))
        elif not reply:
            self.history.pop()
        self._finish_turn(reply)

    def _stream_token(self, token):
        self._stream_buf.append(token)
        self.ui(lambda t=token: self._append_chat(t, "jarvis"))

    # ------------------------------------------------------------------
    # エージェント
    # ------------------------------------------------------------------
    def _agent_state_text(self, task, last_result, step, checklist=None):
        try:
            files = sorted(os.listdir(WORKSPACE_DIR))
        except OSError:
            files = []
        files_disp = "、".join(files) if files else "（まだ何もありません）"
        _h, fg_title, _c = foreground_window()
        titles = list_window_titles()
        titles_disp = "、".join(titles[:12]) if titles else "（取得できませんでした）"
        text = (
            "鳥さんからの頼みごと: %s\n"
            "今の状態（%d手目）:\n"
            "- ジャービスのPCの中のファイル: %s\n"
            "- 今いちばん前のウィンドウ: %s\n"
            "- 開いているウィンドウ: %s\n"
            "- 直前の行動の結果: %s\n"
            % (task, step, files_disp, fg_title or "（不明）", titles_disp,
               last_result or "（まだ何もしていない）")
        )
        if checklist:
            text += "- 要素リスト（これと照らして作ること）:\n%s\n" % checklist
        return text + "\n" + AGENT_FORMAT_HINT

    def _agent_loop(self, task):
        creation = is_creation_request(task)
        system = PERSONA + AGENT_SYSTEM_EXTRA
        if creation:
            system += CREATION_SYSTEM_EXTRA
        pairs = []
        last_result = ""
        final_text = ""
        checklist = None
        try:
            for step in range(1, 16):
                if self.stop_event.is_set():
                    final_text = "途中で止めました。"
                    break
                state = self._agent_state_text(task, last_result, step, checklist)
                turns = []
                for s_state, s_reply in pairs[-4:]:
                    turns.append(("user", s_state))
                    turns.append(("assistant", s_reply))
                turns.append(("user", state))
                buf = []

                def collect(tok, buf=buf):
                    buf.append(tok)

                try:
                    self.client.generate(system, turns, collect, self.stop_event)
                except Exception as e:
                    final_text = "途中でエラーになりました（%s）" % e
                    break
                raw = "".join(buf)
                act = parse_action(raw)
                pairs.append((state, strip_think(raw)[:400]))
                kind = act["type"]

                if kind == "done":
                    final_text = act.get("summary") or "終わりました。"
                    self._post_log("仕事が終わりました")
                    break
                if kind == "text":
                    txt = act.get("text") or ""
                    if creation and checklist is None and txt:
                        # 最初のテキスト返事は「要素リスト」として受け取り、作り始める
                        checklist = txt
                        self._post_jarvis("要素リスト:\n%s" % txt)
                        last_result = ("要素リストを受け取りました。次からは行動行で、"
                                       "部品ごとに実際に作ってください。")
                        continue
                    final_text = txt or "終わりました。"
                    break
                last_result = self._execute_action(act)
                if self.stop_event.is_set():
                    final_text = "途中で止めました。"
                    break
            else:
                final_text = "15手まで進めたので、ここで一度止めます。続きはもう一度頼んでください。"
        finally:
            if final_text:
                self._post_jarvis(final_text)
            self._finish_turn(final_text)

    # ------------------------------------------------------------------
    # 行動の実行（ワーカースレッドから呼ばれる）
    # ------------------------------------------------------------------
    def _confirm(self, title, desc):
        """確認ダイアログ（買い物・破壊的の共用）。ユーザーの答えを待つ。"""
        ev = threading.Event()
        holder = {"ok": False}

        def ask():
            try:
                holder["ok"] = bool(messagebox.askyesno(title, desc, parent=self.root))
            except Exception:
                holder["ok"] = False
            ev.set()

        self.ui(ask)
        ev.wait(timeout=300)
        return holder["ok"]

    def _guard(self, texts, what):
        """
        買い物/破壊的ガード。問題なければ True（続行）。
        止めた場合は理由を返事するための文字列を返す…ではなく、止めたら None を返す。
        """
        joined = " ".join(t for t in texts if t)
        if needs_shopping_confirm(joined):
            if not self._confirm("買い物の確認",
                                 "これは買い物・支払いに関わる操作です。\n\n%s\n\n実行しますか？" % what):
                self._post_log("買い物ガードで止めました: %s" % what)
                return None
        if needs_destructive_confirm(joined):
            if not self._confirm("危ない操作の確認",
                                 "これは電源操作・一括削除など、取り返しのつきにくい操作です。\n\n%s\n\n実行しますか？" % what):
                self._post_log("破壊的ガードで止めました: %s" % what)
                return None
        return True

    _PC_WAIT_KINDS = ("click", "double_click", "right_click", "move", "drag",
                      "key", "type_text", "scroll", "front", "close_window",
                      "maximize", "minimize", "click_name", "see_click")

    def _execute_action(self, act):
        kind = act["type"]
        try:
            result = self._execute_inner(act)
        except Exception as e:
            result = "失敗しました（%s）" % e
            self._post_log(result)
        if kind in self._PC_WAIT_KINDS:
            self.stop_event.wait(0.8)
        return result

    def _execute_inner(self, act):
        kind = act["type"]
        if kind in ("create_file", "edit_file"):
            return self._act_write_file(act, edit=(kind == "edit_file"))
        if kind == "open":
            return self._act_open(act)
        if kind == "app":
            return self._act_app(act)
        if kind == "command":
            return self._act_command(act)
        if kind in ("circle", "line"):
            return self._act_overlay(act)
        if kind in ("click", "double_click", "right_click"):
            return self._act_click(act)
        if kind == "move":
            ok = pc_move(act["x"], act["y"])
            self._post_log("マウスを移動しました（%.0f%%, %.0f%%）" % (act["x"], act["y"]))
            return "マウスを移動しました" if ok else "マウスを動かせませんでした"
        if kind == "drag":
            ok = pc_drag(act["x1"], act["y1"], act["x2"], act["y2"])
            self._post_log("ドラッグしました")
            return "ドラッグしました" if ok else "ドラッグできませんでした"
        if kind == "key":
            return self._act_key(act)
        if kind == "type_text":
            return self._act_type_text(act)
        if kind == "scroll":
            ok = pc_scroll(act["amount"])
            self._post_log("スクロールしました（%d）" % act["amount"])
            return "スクロールしました" if ok else "スクロールできませんでした"
        if kind in ("front", "close_window", "maximize", "minimize"):
            return self._act_window(act)
        if kind == "click_name":
            return self._act_click_name(act)
        if kind == "brightness":
            ok, msg = set_brightness(act["level"])
            self._post_log(msg)
            return msg
        if kind == "screenshot":
            return self._act_screenshot(act)
        if kind == "see":
            return self._act_see(act)
        if kind == "see_click":
            return self._act_see_click(act)
        if kind == "page":
            return self._act_page(act)
        if kind == "wait":
            self.stop_event.wait(act["seconds"])
            return "%.0f秒待ちました" % act["seconds"]
        return "分からない行動でした"

    # -- ファイル系 -------------------------------------------------------
    def _act_write_file(self, act, edit):
        path = safe_join(WORKSPACE_DIR, act.get("name", ""))
        if path is None:
            return "安全のため、その場所には書けませんでした"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        body = act.get("body", "")
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        name = os.path.basename(path)
        self._post_log("ファイル『%s』を%sしました（%d文字）" % (name, "編集" if edit else "作成", len(body)))
        if self.desktop_win is not None:
            self.ui(self.refresh_desktop)
        return "ファイル『%s』を%sしました" % (name, "編集" if edit else "作成")

    def _act_open(self, act):
        name = act.get("name", "")
        if self._guard([name], "開く: %s" % name) is None:
            return "ユーザーが止めました"
        path = safe_join(WORKSPACE_DIR, name)
        if path is None or not os.path.exists(path):
            path = name if os.path.exists(name) else None
        if path is None:
            return "『%s』が見つかりませんでした" % name
        self._post_log("『%s』を開きます" % os.path.basename(path))
        self.ui(lambda p=path: self._open_file_anywhere(p))
        return "『%s』を開きました" % os.path.basename(path)

    def _act_app(self, act):
        target = (act.get("target") or "").strip().strip('"')
        if not target:
            return "アプリ名が空でした"
        if self._guard([target], "アプリ: %s" % target) is None:
            return "ユーザーが止めました"
        self._post_log("『%s』を起動します" % target)
        try:
            if os.path.exists(target):
                open_path(target)
            else:
                hidden_popen(target, shell=True, hide_window=False)
        except Exception as e:
            return "起動できませんでした（%s）" % e
        return "『%s』を起動しました" % target

    def _act_command(self, act):
        cmd = (act.get("cmd") or "").strip()
        if not cmd:
            return "コマンドが空でした"
        if self._guard([cmd], "コマンド: %s" % cmd) is None:
            return "ユーザーが止めました"
        self._post_log("コマンド実行: %s" % cmd)
        try:
            cp = run_hidden(cmd, shell=True, cwd=WORKSPACE_DIR,
                            capture_output=True, timeout=60)
        except subprocess.TimeoutExpired:
            return "コマンドが60秒で終わらなかったので止めました"
        out = _decode(cp.stdout)[:1200]
        err = _decode(cp.stderr)[:400]
        disp = out.strip() or err.strip()
        if disp:
            self._post_log("結果: %s" % disp[:300].replace("\n", " / "))
        result = "終了コード %d。出力: %s" % (cp.returncode, out)
        if err:
            result += " エラー出力: %s" % err
        return result

    def _act_overlay(self, act):
        if act["type"] == "circle":
            self._post_log("画面に丸を描きます（%.0f%%, %.0f%%）" % (act["x"], act["y"]))
            self.ui(lambda: show_circle(self.root, act["x"], act["y"], seconds=4.0))
            label = "画面に丸を描きました"
        else:
            self._post_log("画面に線を描きます")
            self.ui(lambda: show_line(self.root, act["x1"], act["y1"],
                                      act["x2"], act["y2"], seconds=4.0))
            label = "画面に線を描きました"
        self.stop_event.wait(4.3)
        return label

    # -- PC操作系 -----------------------------------------------------------
    def _guard_click_target(self, what):
        """クリック系: 前面ウィンドウの題名が買い物らしい場合は確認する。"""
        _h, fg_title, _c = foreground_window()
        if needs_shopping_confirm(fg_title):
            if not self._confirm("買い物の確認",
                                 "今いちばん前のウィンドウ（%s）が買い物に関係しそうです。\n\n%s\n\n実行しますか？"
                                 % (fg_title, what)):
                self._post_log("買い物ガードで止めました: %s" % what)
                return False
        return True

    def _act_click(self, act):
        kind = act["type"]
        label = {"click": "クリック", "double_click": "ダブルクリック",
                 "right_click": "右クリック"}[kind]
        if not self._guard_click_target("%s（%.0f%%, %.0f%%）" % (label, act["x"], act["y"])):
            return "ユーザーが止めました"
        ok = pc_click(act["x"], act["y"],
                      button="right" if kind == "right_click" else "left",
                      double=(kind == "double_click"))
        self._post_log("%sしました（%.0f%%, %.0f%%）" % (label, act["x"], act["y"]))
        return "%sしました" % label if ok else "%sできませんでした" % label

    def _act_key(self, act):
        spec = act.get("spec", "")
        vks = key_spec_to_vk(spec)
        if not vks:
            return "キー『%s』が分かりませんでした" % spec
        ok = pc_key(vks)
        self._post_log("キーを押しました: %s" % spec)
        return "キー %s を押しました" % spec if ok else "キーを押せませんでした"

    def _act_type_text(self, act):
        text = act.get("text", "")
        if not text:
            return "入力する文字が空でした"
        if self._guard([text], "入力: %s" % text[:40]) is None:
            return "ユーザーが止めました"
        ok = pc_type_text(text)
        self._post_log("入力しました: %s" % text[:40])
        return "入力しました（クリップボード経由で貼り付けました）" if ok else "入力できませんでした"

    def _act_window(self, act):
        action = {"front": "front", "close_window": "close",
                  "maximize": "maximize", "minimize": "minimize"}[act["type"]]
        label = {"front": "前面に出す", "close_window": "閉じる",
                 "maximize": "最大化", "minimize": "最小化"}[act["type"]]
        ok, found = window_action(act.get("title", ""), action)
        if ok:
            self._post_log("『%s』を%sしました" % (found, label))
            return "『%s』を%sしました" % (found, label)
        self._post_log(found)
        return found

    def _act_click_name(self, act):
        name = act.get("name", "")
        if not name:
            return "名前が空でした"
        if not self._guard_click_target("名前をクリック: %s" % name):
            return "ユーザーが止めました"
        self._post_log("『%s』を探して押します…" % name)
        ok, found = uia_click_by_name(name)
        if ok:
            self._post_log("『%s』を押しました" % found)
            return "『%s』を押しました" % found
        self._post_log("押せませんでした: %s" % found)
        return "『%s』を押せませんでした（%s）" % (name, found)

    def _act_screenshot(self, act):
        name = act.get("name") or "スクショ.png"
        if not name.lower().endswith(".png"):
            name += ".png"
        path = safe_join(WORKSPACE_DIR, name)
        if path is None:
            return "安全のため、その場所には保存できませんでした"
        if capture_screen_to(path):
            self._post_log("スクショを『%s』に保存しました" % os.path.basename(path))
            if self.desktop_win is not None:
                self.ui(self.refresh_desktop)
            return "スクショをジャービスのPCの『%s』に保存しました" % os.path.basename(path)
        return "スクショを撮れませんでした（Pillow が無いかもしれません）"

    # -- 視覚系 ---------------------------------------------------------------
    def _vision_guard_ready(self):
        if not self.vision_ready:
            return ("目はまだ設定されていません。設定で Vision モデルと"
                    "目のファイル（mmproj）を選ぶと、画面が見えるようになります。")
        return None

    def _act_see(self, act):
        note = self._vision_guard_ready()
        if note:
            self._post_log(note)
            return note
        question = act.get("question") or "この画面を説明して"
        self._post_log("画面を見ています…")
        self.ui(lambda: self.globe.set_active(True))
        b64 = screenshot_b64()
        if b64 is None:
            return "画面を撮れませんでした"
        try:
            ans = self.client.vision_ask(question, b64)
        except Exception as e:
            return "目で見るのに失敗しました（%s）" % e
        self._post_log("画面を見ました")
        return "画面を見た答え: %s" % (ans or "（答えが返りませんでした）")[:1500]

    def _act_see_click(self, act):
        note = self._vision_guard_ready()
        if note:
            self._post_log(note)
            return note
        name = act.get("name", "")
        if not name:
            return "対象の名前が空でした"
        if not self._guard_click_target("見てクリック: %s" % name):
            return "ユーザーが止めました"
        self._post_log("『%s』を目で探しています…" % name)
        b64 = screenshot_b64()
        if b64 is None:
            return "画面を撮れませんでした"
        prompt = ("画面の中にある「%s」の中心の座標を、0から1000の物差しで答えてください。"
                  "数字を2つだけ、半角スペース区切りで答えてください。説明は書かないでください。" % name)
        try:
            ans = self.client.vision_ask(prompt, b64)
        except Exception as e:
            return "目で探すのに失敗しました（%s）" % e
        coords = parse_vision_coords(ans)
        if coords is None:
            self._post_log("『%s』の場所が分かりませんでした" % name)
            return "『%s』の場所が分かりませんでした（モデルの答え: %s）" % (name, (ans or "")[:80])
        x, y = coords
        ok = pc_click(x, y)
        self._post_log("『%s』をクリックしました（%.0f%%, %.0f%%）" % (name, x, y))
        return "『%s』をクリックしました" % name if ok else "クリックできませんでした"

    # -- ページ系 ---------------------------------------------------------------
    def _ask_with_text_model(self, system, user_text):
        buf = []
        self.client.generate(system, [("user", user_text)],
                             lambda t: buf.append(t), self.stop_event)
        return strip_think("".join(buf))

    def _page_answer(self, question):
        """ページを読んで答える。(成功か, 答え or 理由)"""
        ctx = get_page_context()
        if not ctx.get("ok"):
            if ctx.get("reason") == "not_browser":
                return (False, "今いちばん前にあるのはブラウザではないようです。"
                                "画面のことは「画面を見る」で聞いてください。")
            return (False, "ページの文章を取れませんでした。")
        self._post_log("ページを読みました（%s経由）" % ctx.get("route", ""))
        q = build_page_question(question, ctx["title"], ctx["url"], ctx["text"])
        try:
            ans = self._ask_with_text_model(PERSONA, q)
        except Exception as e:
            return (False, "ページの説明に失敗しました（%s）" % e)
        return (True, ans or "（答えが返りませんでした）")

    def _act_page(self, act):
        question = act.get("question") or "このページを説明して"
        ok, ans = self._page_answer(question)
        if not ok:
            self._post_log(ans)
            return ans
        return "ページを読んだ答え: %s" % ans[:1500]


    # ------------------------------------------------------------------
    # 吹き出しのボタン: 画面を見る / ページを説明 / 見せて説明
    # ------------------------------------------------------------------
    def _on_see_screen(self):
        question = self.input_entry.get().strip() or "この画面を説明して"
        self.input_entry.delete(0, "end")
        if self.busy:
            self._post_sys("今、前の仕事をしています。")
            return
        note = self._vision_guard_ready()
        if note:
            self._post_err(note)
            return
        if not self.server.healthy():
            self._post_err("サーバーが準備できていません。")
            return
        self._post_user("（画面を見て）%s" % question)
        self.busy = True
        self.stop_event.clear()
        self.globe.set_active(True)
        threading.Thread(target=self._see_screen_thread, args=(question,), daemon=True).start()

    def _see_screen_thread(self, question):
        try:
            self._post_log("画面を見ています…")
            b64 = screenshot_b64()
            if b64 is None:
                self._post_err("画面を撮れませんでした。")
                self._finish_turn("")
                return
            ans = self.client.vision_ask(question, b64)
            if ans:
                self._post_jarvis(ans)
                self.history.append(("user", "（画面を見せて）%s" % question))
                self.history.append(("assistant", ans))
            self._finish_turn(ans)
        except Exception as e:
            self._post_err("目で見るのに失敗しました（%s）" % e)
            self._finish_turn("")

    def _on_explain_page(self):
        question = self.input_entry.get().strip() or "このページを説明して"
        self.input_entry.delete(0, "end")
        if self.busy:
            self._post_sys("今、前の仕事をしています。")
            return
        if not self.server.healthy():
            self._post_err("サーバーが準備できていません。")
            return
        self._post_user("（ページを読んで）%s" % question)
        self.busy = True
        self.stop_event.clear()
        self.globe.set_active(True)
        threading.Thread(target=self._explain_page_thread, args=(question,), daemon=True).start()

    def _explain_page_thread(self, question):
        try:
            ok, ans = self._page_answer(question)
            if ok:
                self._post_jarvis(ans)
                self._finish_turn(ans)
            else:
                self._post_err(ans)
                self._finish_turn("")
        except Exception as e:
            self._post_err("ページの説明に失敗しました（%s）" % e)
            self._finish_turn("")

    def _on_explain_titles(self):
        question = self.input_entry.get().strip() or "今の画面について説明して"
        self.input_entry.delete(0, "end")
        titles = list_window_titles()
        shot_path = os.path.join(SHOTS_DIR, "last_screen.png")
        if capture_screen_to(shot_path):
            self._show_screenshot(shot_path)
        else:
            self._post_sys("画面の撮影はできませんでした（Pillow が無いかもしれません）。")
        ctx = "、".join(titles) if titles else "（ウィンドウ一覧を取得できませんでした）"
        msg = ("今、鳥さんの画面で開いているウィンドウの一覧です: %s\n"
               "この画面について、鳥さんの質問に答えてください。必要なら丸や線で場所を指せます。\n"
               "質問: %s" % (ctx, question))
        self._dispatch_user_message(msg, display_text="（見せて説明）%s" % question)

    def _show_screenshot(self, path):
        def make():
            try:
                win = tk.Toplevel(self.root)
                win.title("今の画面")
                label = tk.Label(win, bg="#0a1420")
                label.pack()
                img = None
                try:
                    from PIL import Image, ImageTk
                    pil = Image.open(path)
                    pil.thumbnail((880, 560))
                    img = ImageTk.PhotoImage(pil)
                except Exception:
                    img = tk.PhotoImage(file=path)
                    w, h = img.width(), img.height()
                    f = max(1, max(w // 880, h // 560))
                    if f > 1:
                        img = img.subsample(f, f)
                label.configure(image=img)
                label.image = img
            except Exception as e:
                self._post_err("画面を表示できませんでした（%s）" % e)
        self.ui(make)

    # ------------------------------------------------------------------
    # ジャービスのPC（仮想デスクトップ窓）
    # ------------------------------------------------------------------
    def _open_desktop(self):
        if self.desktop_win is not None:
            try:
                self.desktop_win.deiconify()
                self.desktop_win.lift()
                self.refresh_desktop()
                return
            except tk.TclError:
                self.desktop_win = None
        win = tk.Toplevel(self.root)
        win.title("ジャービスのPC")
        win.geometry("760x520")
        win.configure(bg="#0a1420")
        self.desktop_win = win
        top = tk.Frame(win, bg="#0a1420")
        top.pack(fill="x", padx=8, pady=6)
        tk.Label(top, text="ここはジャービス専用のパソコンです。作ったファイルはここに並びます。",
                 bg="#0a1420", fg="#d7f3ff", font=("Yu Gothic UI", 10)).pack(side="left")
        ttk.Button(top, text="更新", command=self.refresh_desktop).pack(side="right")
        ttk.Button(top, text="フォルダを開く",
                   command=lambda: self._safe_open(WORKSPACE_DIR)).pack(side="right", padx=4)
        self.desktop_frame = tk.Frame(win, bg="#0e2a3a")
        self.desktop_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        def on_close():
            self.desktop_win = None
            self.desktop_frame = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close)
        self.refresh_desktop()

    def refresh_desktop(self):
        if self.desktop_frame is None:
            return
        try:
            if not self.desktop_frame.winfo_exists():
                return
        except tk.TclError:
            return
        for child in self.desktop_frame.winfo_children():
            child.destroy()
        try:
            entries = sorted(os.listdir(WORKSPACE_DIR))
        except OSError:
            entries = []
        if not entries:
            tk.Label(self.desktop_frame,
                     text="まだ何もありません。チャットで頼むと、ジャービスがここに作ります。",
                     bg="#0e2a3a", fg="#9fc6d4", font=("Yu Gothic UI", 10)).grid(
                         row=0, column=0, padx=16, pady=16, sticky="w")
            return
        cols = 6
        for i, name in enumerate(entries):
            path = os.path.join(WORKSPACE_DIR, name)
            is_dir = os.path.isdir(path)
            ext = os.path.splitext(name)[1].lower()
            if is_dir:
                mark, fg = "■", "#ffd98a"
            elif ext in IMAGE_EXTS:
                mark, fg = "▣", "#9fe8b1"
            elif ext in TEXT_EXTS:
                mark, fg = "▤", "#d7f3ff"
            else:
                mark, fg = "▢", "#b8ccd4"
            lbl = tk.Label(self.desktop_frame, text="%s\n%s" % (mark, name),
                           bg="#0e2a3a", fg=fg, font=("Yu Gothic UI", 9),
                           wraplength=110, justify="center", cursor="hand2",
                           padx=6, pady=8)
            lbl.grid(row=i // cols, column=i % cols, padx=6, pady=6, sticky="nsew")
            lbl.bind("<Double-Button-1>", lambda e, p=path: self._open_file_anywhere(p))

    def _safe_open(self, path):
        try:
            open_path(path)
        except Exception as e:
            self._post_err("開けませんでした（%s）" % e)

    def _open_file_anywhere(self, path):
        try:
            if os.path.isdir(path):
                open_path(path)
                return
            ext = os.path.splitext(path)[1].lower()
            if ext in TEXT_EXTS:
                self._open_text_viewer(path)
            elif ext in IMAGE_EXTS:
                self._open_image_viewer(path)
            else:
                open_path(path)
        except Exception as e:
            self._post_err("開けませんでした（%s）" % e)

    def _open_text_viewer(self, path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError as e:
            self._post_err("読めませんでした（%s）" % e)
            return
        win = tk.Toplevel(self.root)
        win.title(os.path.basename(path))
        win.geometry("640x480")
        txt = scrolledtext.ScrolledText(win, wrap="word", font=("Yu Gothic UI", 11),
                                        bg="#0d1b2a", fg="#eaf7ff",
                                        insertbackground="#eaf7ff")
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", content)

        def save():
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(txt.get("1.0", "end-1c"))
                messagebox.showinfo("保存", "保存しました", parent=win)
            except OSError as e:
                messagebox.showerror("保存", "保存できませんでした（%s）" % e, parent=win)

        ttk.Button(win, text="保存", command=save).pack(fill="x")

    def _open_image_viewer(self, path):
        win = tk.Toplevel(self.root)
        win.title(os.path.basename(path))
        label = tk.Label(win, bg="#0a1420")
        label.pack(fill="both", expand=True)
        img = None
        try:
            from PIL import Image, ImageTk
            pil = Image.open(path)
            pil.thumbnail((900, 640))
            img = ImageTk.PhotoImage(pil)
        except Exception:
            try:
                img = tk.PhotoImage(file=path)
            except Exception:
                pass
        if img is None:
            win.destroy()
            self._safe_open(path)
            return
        label.configure(image=img)
        label.image = img

    # ------------------------------------------------------------------
    # 設定窓
    # ------------------------------------------------------------------
    def _open_settings(self):
        if self.settings_win is not None:
            try:
                self.settings_win.deiconify()
                self.settings_win.lift()
                return
            except tk.TclError:
                self.settings_win = None
        win = tk.Toplevel(self.root)
        win.title("ジャービスの設定")
        win.geometry("720x560")
        win.configure(bg="#0a1420")
        self.settings_win = win
        f = win
        pad = {"padx": 10, "pady": 5}

        def browse(var, title, patterns):
            path = filedialog.askopenfilename(
                title=title, filetypes=patterns, parent=win)
            if path:
                var.set(path)
                self._save_cfg()

        tk.Label(f, text="モデル（GGUF）", bg="#0a1420", fg="#d7f3ff",
                 font=("Yu Gothic UI", 10, "bold")).grid(row=0, column=0, sticky="w", **pad)
        tk.Entry(f, textvariable=self.model_var, width=58, bg="#12283d", fg="#eaf7ff",
                 insertbackground="#eaf7ff", relief="flat").grid(row=0, column=1, sticky="we", **pad)
        ttk.Button(f, text="参照…",
                   command=lambda: browse(self.model_var, "モデル（GGUF）を選んでください",
                                          [("GGUFモデル", "*.gguf"), ("すべて", "*.*")])
                   ).grid(row=0, column=2, **pad)

        tk.Label(f, text="目のファイル（mmproj）", bg="#0a1420", fg="#d7f3ff",
                 font=("Yu Gothic UI", 10, "bold")).grid(row=1, column=0, sticky="w", **pad)
        tk.Entry(f, textvariable=self.mmproj_var, width=58, bg="#12283d", fg="#eaf7ff",
                 insertbackground="#eaf7ff", relief="flat").grid(row=1, column=1, sticky="we", **pad)
        ttk.Button(f, text="参照…",
                   command=lambda: browse(self.mmproj_var, "目のファイル（mmproj GGUF）を選んでください",
                                          [("GGUF", "*.gguf"), ("すべて", "*.*")])
                   ).grid(row=1, column=2, **pad)
        tk.Label(f, text="任意です。Vision モデルとセットで入れると、画面が見えるようになります。",
                 bg="#0a1420", fg="#7fa8b8", font=("Yu Gothic UI", 8)).grid(
                     row=2, column=1, sticky="w", padx=10)

        tk.Label(f, text=BRAIN_HINT, bg="#0a1420", fg="#8fd8e8", font=("Yu Gothic UI", 9),
                 justify="left").grid(row=3, column=0, columnspan=3, sticky="w", padx=10, pady=8)

        tk.Label(f, text="VOICEVOX 話者ID", bg="#0a1420", fg="#d7f3ff",
                 font=("Yu Gothic UI", 10, "bold")).grid(row=4, column=0, sticky="w", **pad)
        tk.Entry(f, textvariable=self.speaker_var, width=10, bg="#12283d", fg="#eaf7ff",
                 insertbackground="#eaf7ff", relief="flat").grid(row=4, column=1, sticky="w", **pad)
        tk.Label(f, text="既定は 11（玄野武宏）。", bg="#0a1420", fg="#7fa8b8",
                 font=("Yu Gothic UI", 8)).grid(row=4, column=2, sticky="w", **pad)

        tk.Label(f, textvariable=self.voicevox_status_var, bg="#0a1420", fg="#8fd8e8",
                 font=("Yu Gothic UI", 9)).grid(row=5, column=0, columnspan=2, sticky="w", **pad)
        ttk.Button(f, text="VOICEVOXを再確認", command=lambda: threading.Thread(
            target=self._detect_voicevox_thread, daemon=True).start()).grid(row=5, column=2, **pad)

        tk.Label(f, textvariable=self.server_status_var, bg="#0a1420", fg="#8fd8e8",
                 font=("Yu Gothic UI", 9)).grid(row=6, column=0, sticky="w", **pad)
        ttk.Button(f, text="サーバー起動", command=self._start_server_async).grid(row=6, column=1, sticky="w", **pad)
        ttk.Button(f, text="サーバー停止", command=self._stop_server).grid(row=6, column=2, sticky="w", **pad)

        ttk.Checkbutton(f, text="声で読み上げる", variable=self.voice_var,
                        command=self._save_cfg).grid(row=7, column=0, sticky="w", **pad)
        ttk.Checkbutton(f, text="エージェントモード", variable=self.agent_var,
                        command=self._save_cfg).grid(row=7, column=1, sticky="w", **pad)
        ttk.Checkbutton(f, text="ずっと聞き耳", variable=self.listen_var,
                        command=lambda: self._set_listen(bool(self.listen_var.get()))
                        ).grid(row=8, column=0, sticky="w", **pad)
        ttk.Checkbutton(f, text="Windows起動時に自動で出す", variable=self.autostart_var,
                        command=self._on_autostart_toggle).grid(row=8, column=1, sticky="w", **pad)
        ttk.Button(f, text="設定を保存", command=self._save_and_maybe_restart).grid(
            row=9, column=0, sticky="w", **pad)
        tk.Label(f, text="モデルや目を変えたら、保存でサーバーが自動で起動し直します。",
                 bg="#0a1420", fg="#7fa8b8", font=("Yu Gothic UI", 8)).grid(
                     row=9, column=1, columnspan=2, sticky="w", **pad)

        note = ("完全オフライン: 通信はこのパソコンの中（127.0.0.1）だけです。聞き耳も端末の中だけです。\n"
                "黒いコンソール窓は出ません。中の処理は全部、窓なしで動いています。")
        tk.Label(f, text=note, bg="#0a1420", fg="#7fa8b8", font=("Yu Gothic UI", 9),
                 justify="left").grid(row=10, column=0, columnspan=3, sticky="w", padx=10, pady=14)
        f.columnconfigure(1, weight=1)

        def on_close():
            self.settings_win = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close)

    def _save_and_maybe_restart(self):
        self._save_cfg()
        current = (self.model_var.get(), self.mmproj_var.get())
        if self._started_with is not None and self._started_with != current:
            self._post_sys("モデル設定が変わったので、サーバーを起動し直します。")
            self.server.stop()
            self._started_with = None
            self._start_server_async()
        elif self._started_with is None and current[0]:
            self._start_server_async()
        self._post_sys("設定を保存しました。")

    def _choose_model_dialog(self):
        path = filedialog.askopenfilename(
            title="モデル（GGUF）を選んでください",
            initialdir=MODELS_DIR if os.path.isdir(MODELS_DIR) else DATA_DIR,
            filetypes=[("GGUFモデル", "*.gguf"), ("すべて", "*.*")])
        if path:
            self.model_var.set(path)
            self._save_cfg()
            self._start_server_async()

    # ------------------------------------------------------------------
    # 終了
    # ------------------------------------------------------------------
    def _on_close(self):
        self._save_cfg()
        try:
            self.listener.stop()
        except Exception:
            pass
        try:
            self.voice.stop()
        except Exception:
            pass
        try:
            self.server.stop()
        except Exception:
            pass
        self.root.destroy()


def main():
    root = tk.Tk()
    root.withdraw()  # 球の窓として作り直すので、一瞬も素の窓を見せない
    app = JarvisApp(root)
    root.deiconify()
    root.mainloop()


if __name__ == "__main__":
    main()
