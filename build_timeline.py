"""
Claude Code 全プロンプト時系列抽出ツール（リッチ版）

処理:
  1. ~/.claude/projects/**/*.jsonl を全部走査
  2. セッション単位でメッセージを時系列処理
  3. 各ユーザー発話に以下のコンテキストを紐付け:
     - actions: ツール種別ごとの呼び出し回数
     - files_written / files_edited / files_read
     - first_reply: アシスタントの最初のテキスト返信（200字まで）
     - turn_count: 次のユーザー発話までの assistant 往復数
     - duration_sec: 所要時間
     - status: completed / interrupted / errors
     - branch / session / entrypoint
  4. タイムスタンプ昇順にソートしてJSON化
  5. パスフレーズ(AES-GCM/PBKDF2-SHA256)で暗号化してsite/data.enc.jsonへ
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:
    sys.stderr.write("cryptography が要ります: pip install cryptography\n")
    sys.exit(1)

PROJECTS_DIR = Path.home() / ".claude" / "projects"
OUT_DIR = Path(__file__).parent / "site"
SUMMARIES_DIR = Path(__file__).parent / "summaries"
ITER = 200_000

NOISE_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<command-stdout>",
    "<command-stderr>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
    "Caveat:",
)

INTERRUPT_PATTERN = re.compile(r"\[Request interrupted")

TOOL_CATEGORY = {
    "Bash": "bash",
    "PowerShell": "bash",
    "Edit": "edit",
    "Write": "write",
    "Read": "read",
    "NotebookEdit": "edit",
    "Glob": "search",
    "Grep": "search",
    "ToolSearch": "search",
    "WebFetch": "web",
    "WebSearch": "web",
    "Task": "agent",
    "Agent": "agent",
    "TodoWrite": "todo",
    "ScheduleWakeup": "schedule",
    "AskUserQuestion": "ask",
    "EnterPlanMode": "plan",
    "ExitPlanMode": "plan",
}


def cwd_to_project(cwd: str | None) -> str:
    if not cwd:
        return "(unknown)"
    parts = Path(cwd).parts
    if len(parts) >= 2:
        return parts[-1] if parts[-1] else parts[-2]
    return cwd


def categorize_tool(name: str) -> str:
    if not name:
        return "other"
    if name.startswith("mcp__"):
        return "mcp"
    return TOOL_CATEGORY.get(name, "other")


def extract_text_blocks(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    out = []
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                t = c.get("text", "")
                if t:
                    out.append(t)
    return out


def is_real_user_prompt(text: str) -> bool:
    if not text:
        return False
    t = text.lstrip()
    for p in NOISE_PREFIXES:
        if t.startswith(p):
            return False
    return True


def new_prompt_ctx(ts: str, cwd: str | None, session: str | None,
                   branch: str | None, entry: str | None, text: str) -> dict:
    return {
        "ts": ts,
        "project": cwd_to_project(cwd),
        "cwd": cwd,
        "session": session,
        "branch": branch,
        "entry": entry,
        "text": text,
        "actions": {},
        "files_written": [],
        "files_edited": [],
        "files_read": [],
        "first_reply": "",
        "turn_count": 0,
        "duration_sec": 0,
        "end_ts": ts,
        "interrupted": False,
        "errors": 0,
        "agents": [],
    }


def parse_ts(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def process_session(path: Path, records: list[dict]) -> None:
    """Iterate one JSONL session, emit enriched user-prompt records."""
    current: dict | None = None

    def flush():
        if current is None:
            return
        # duration
        t0 = parse_ts(current["ts"])
        t1 = parse_ts(current["end_ts"])
        if t0 and t1:
            current["duration_sec"] = max(0, int((t1 - t0).total_seconds()))
        # dedupe file lists, keep order
        for k in ("files_written", "files_edited", "files_read", "agents"):
            seen, out = set(), []
            for v in current[k]:
                if v and v not in seen:
                    seen.add(v)
                    out.append(v)
            current[k] = out
        records.append(current)

    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = d.get("type")

                if t == "user":
                    msg = d.get("message") or {}
                    if msg.get("role") != "user":
                        continue
                    content = msg.get("content")
                    # tool_result messages embedded as user — track errors
                    if isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "tool_result":
                                if current is not None:
                                    if c.get("is_error"):
                                        current["errors"] += 1
                                    if d.get("timestamp"):
                                        current["end_ts"] = d["timestamp"]
                    # real user text?
                    texts = extract_text_blocks(content)
                    joined = "\n".join(texts).strip()
                    if joined and is_real_user_prompt(joined):
                        if INTERRUPT_PATTERN.search(joined) and current is not None:
                            current["interrupted"] = True
                            current["end_ts"] = d.get("timestamp", current["end_ts"])
                            continue  # not a new user prompt, just a marker
                        # new real prompt — flush previous, open new
                        flush()
                        current = new_prompt_ctx(
                            ts=d.get("timestamp", ""),
                            cwd=d.get("cwd"),
                            session=d.get("sessionId"),
                            branch=d.get("gitBranch"),
                            entry=d.get("entrypoint"),
                            text=joined,
                        )

                elif t == "assistant" and current is not None:
                    current["turn_count"] += 1
                    if d.get("timestamp"):
                        current["end_ts"] = d["timestamp"]
                    msg = d.get("message") or {}
                    for c in msg.get("content") or []:
                        if not isinstance(c, dict):
                            continue
                        ctype = c.get("type")
                        if ctype == "text":
                            txt = (c.get("text") or "").strip()
                            if txt and not current["first_reply"]:
                                current["first_reply"] = txt[:240]
                        elif ctype == "tool_use":
                            name = c.get("name", "")
                            cat = categorize_tool(name)
                            current["actions"][cat] = current["actions"].get(cat, 0) + 1
                            inp = c.get("input") or {}
                            if name in ("Write",):
                                fp = inp.get("file_path")
                                if fp:
                                    current["files_written"].append(fp)
                            elif name in ("Edit", "NotebookEdit"):
                                fp = inp.get("file_path")
                                if fp:
                                    current["files_edited"].append(fp)
                            elif name == "Read":
                                fp = inp.get("file_path")
                                if fp:
                                    current["files_read"].append(fp)
                            elif name in ("Task", "Agent"):
                                desc = inp.get("description") or inp.get("subagent_type") or ""
                                if desc:
                                    current["agents"].append(desc[:80])
    except Exception as e:
        sys.stderr.write(f"skip {path.name}: {e}\n")

    flush()


def collect() -> list[dict]:
    if not PROJECTS_DIR.exists():
        sys.stderr.write(f"ログ無い: {PROJECTS_DIR}\n")
        sys.exit(1)
    files = sorted(PROJECTS_DIR.rglob("*.jsonl"))
    sys.stderr.write(f"{len(files)}ファイル走査\n")
    records: list[dict] = []
    for fp in files:
        process_session(fp, records)
    records.sort(key=lambda r: r["ts"])
    sys.stderr.write(f"実プロンプト数: {len(records)}\n")
    return records


def load_summaries() -> list[dict]:
    if not SUMMARIES_DIR.exists():
        return []
    out = []
    for p in sorted(SUMMARIES_DIR.glob("*.md")):
        try:
            txt = p.read_text(encoding="utf-8").strip()
            out.append({"week": p.stem, "text": txt})
        except Exception:
            continue
    return out


def encrypt(plain_bytes: bytes, passphrase: str) -> dict:
    salt = os.urandom(16)
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITER
    )
    key = kdf.derive(passphrase.encode("utf-8"))
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plain_bytes, None)
    return {
        "v": 2,
        "kdf": "PBKDF2-SHA256",
        "iter": ITER,
        "salt": base64.b64encode(salt).decode(),
        "nonce": base64.b64encode(nonce).decode(),
        "ct": base64.b64encode(ct).decode(),
    }


def main() -> None:
    passphrase = os.environ.get("TIMELINE_PASS")
    if not passphrase:
        sys.stderr.write("環境変数 TIMELINE_PASS を設定してや\n")
        sys.exit(2)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    records = collect()
    payload = {
        "built_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "count": len(records),
        "records": records,
        "summaries": load_summaries(),
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    enc = encrypt(raw, passphrase)
    (OUT_DIR / "data.enc.json").write_text(
        json.dumps(enc, separators=(",", ":")), encoding="utf-8"
    )
    sys.stderr.write(
        f"OK: {OUT_DIR / 'data.enc.json'}  ({len(raw):,}B -> {len(enc['ct']):,}B b64)\n"
    )


if __name__ == "__main__":
    main()
