"""
Claude Code 全プロンプト時系列抽出ツール

処理:
  1. ~/.claude/projects/**/*.jsonl を全部走査
  2. type=user かつ実ユーザー発話だけ抽出（tool_result や system-reminder は除外）
  3. タイムスタンプ順にソートしてJSON化
  4. パスフレーズ(AES-GCM/PBKDF2-SHA256)で暗号化してdata.enc.jsonへ
  5. index.htmlはそのまま(テンプレ運用)

なぜ暗号化:
  GitHub Pages無料枠は公開リポ必須。URLが漏れても中身が読めない設計にする。
"""
from __future__ import annotations

import base64
import json
import os
import sys
from datetime import datetime
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
ITER = 200_000  # PBKDF2反復

# system-reminderやtool結果など「ユーザーが本当に打った訳ではない」プレフィックスを弾く
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
    "[Request interrupted",
)


def cwd_to_project(cwd: str | None) -> str:
    if not cwd:
        return "(unknown)"
    # 長いフルパスを短縮: c:\Users\atsus\000_ClaudeCode\004_MuscleLove → 004_MuscleLove
    parts = Path(cwd).parts
    if len(parts) >= 2:
        return parts[-1] if parts[-1] else parts[-2]
    return cwd


def extract_text(msg: dict) -> str | None:
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        chunks = []
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text":
                txt = c.get("text", "")
                if txt:
                    chunks.append(txt)
        if chunks:
            return "\n".join(chunks).strip() or None
    return None


def is_real_user_prompt(text: str) -> bool:
    if not text:
        return False
    t = text.lstrip()
    for p in NOISE_PREFIXES:
        if t.startswith(p):
            return False
    # ペーストされた長大なツール出力の雰囲気（改行多 & コード的）は一旦残す
    return True


def collect() -> list[dict]:
    if not PROJECTS_DIR.exists():
        sys.stderr.write(f"ログ無い: {PROJECTS_DIR}\n")
        sys.exit(1)

    records: list[dict] = []
    files = sorted(PROJECTS_DIR.rglob("*.jsonl"))
    sys.stderr.write(f"{len(files)}ファイル走査\n")

    for fp in files:
        try:
            with fp.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("type") != "user":
                        continue
                    if d.get("isMeta"):
                        continue
                    msg = d.get("message") or {}
                    if msg.get("role") != "user":
                        continue
                    # tool_resultしか無い場合はcontent listにtextが無いので弾かれる
                    text = extract_text(msg)
                    if not text or not is_real_user_prompt(text):
                        continue
                    ts = d.get("timestamp")
                    if not ts:
                        continue
                    records.append({
                        "ts": ts,
                        "project": cwd_to_project(d.get("cwd")),
                        "cwd": d.get("cwd"),
                        "session": d.get("sessionId"),
                        "branch": d.get("gitBranch"),
                        "entry": d.get("entrypoint"),
                        "text": text,
                    })
        except Exception as e:
            sys.stderr.write(f"skip {fp.name}: {e}\n")

    # ts昇順。同じtsなら安定
    records.sort(key=lambda r: r["ts"])
    sys.stderr.write(f"実プロンプト数: {len(records)}\n")
    return records


def encrypt(plain_bytes: bytes, passphrase: str) -> dict:
    salt = os.urandom(16)
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITER
    )
    key = kdf.derive(passphrase.encode("utf-8"))
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plain_bytes, None)
    return {
        "v": 1,
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
        "built_at": datetime.utcnow().isoformat() + "Z",
        "count": len(records),
        "records": records,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    enc = encrypt(raw, passphrase)
    (OUT_DIR / "data.enc.json").write_text(
        json.dumps(enc, separators=(",", ":")), encoding="utf-8"
    )
    sys.stderr.write(
        f"OK: {OUT_DIR / 'data.enc.json'}  ({len(raw):,}B → {len(enc['ct']):,}B b64)\n"
    )


if __name__ == "__main__":
    main()
