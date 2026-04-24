"""
週次AI要約生成スクリプト

毎週日曜07:00に Task Scheduler から呼ばれる想定:
  1. build_timeline.py の抽出ロジックを再利用してプロンプト一覧取得
  2. ISO週単位でグルーピング
  3. 完了済み週(=過去週)のうち summaries/<week>.md が無いものを対象に
  4. プロンプトの要点を短く抽出して claude CLI (Max枠) に投げ
  5. 返ってきた要約を summaries/<week>.md に保存
  6. 最後に build_timeline.py を再実行して再暗号化+push

claude CLI を使うので API キー不要・Claude Max の枠で完結する。
"""
from __future__ import annotations

import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from build_timeline import collect  # noqa: E402

SUMMARIES_DIR = Path(__file__).parent / "summaries"
CLAUDE_BIN = "claude"
TIMEOUT = 600  # claude -p のタイムアウト


def iso_week(dt: datetime) -> str:
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def week_bounds(week_str: str) -> tuple[date, date]:
    year, w = week_str.split("-W")
    d = date.fromisocalendar(int(year), int(w), 1)  # Mon
    return d, d + timedelta(days=6)


def summarize_week(week: str, records: list[dict]) -> str:
    d0, d1 = week_bounds(week)
    # per-project stats
    per = defaultdict(lambda: {"n":0, "files":set(), "samples":[], "dur":0, "errs":0, "ints":0})
    for r in records:
        p = per[r["project"]]
        p["n"] += 1
        p["dur"] += r.get("duration_sec", 0)
        p["errs"] += 1 if r.get("errors", 0)>0 else 0
        p["ints"] += 1 if r.get("interrupted") else 0
        for f in (r.get("files_written", []) + r.get("files_edited", [])):
            p["files"].add(Path(f).name)
        if len(p["samples"]) < 3:
            p["samples"].append(r["text"][:100].replace("\n"," "))

    # Build concise context
    lines = [f"# {week} ({d0}〜{d1}) 作業ログ要約依頼", ""]
    lines.append(f"プロンプト総数: {len(records)}")
    lines.append("")
    lines.append("## プロジェクト別")
    for proj, p in sorted(per.items(), key=lambda x:-x[1]["n"]):
        files_s = ", ".join(list(p["files"])[:8])
        lines.append(f"- **{proj}**: {p['n']}件 / {p['dur']//60}分 / err{p['errs']} 中断{p['ints']} / 触ったファイル: {files_s}")
        for s in p["samples"]:
            lines.append(f"    - 例: {s}")
    lines.append("")
    lines.append("この週の作業を以下の構造で簡潔に日本語要約してください:")
    lines.append("- **何をやったか**: 2-3行で要点")
    lines.append("- **主な成果物**: 完成した/進んだファイルやサイト")
    lines.append("- **未完了/気になる点**: エラー・中断があった箇所や引継ぎが必要そうな作業")
    lines.append("")
    lines.append("300字以内、Markdown箇条書きで。")
    prompt = "\n".join(lines)

    try:
        r = subprocess.run(
            [CLAUDE_BIN, "-p", prompt],
            capture_output=True, text=True, timeout=TIMEOUT, encoding="utf-8"
        )
    except FileNotFoundError:
        return f"(claude CLI 見つからず。手動確認要) — プロンプト数:{len(records)}"
    except subprocess.TimeoutExpired:
        return f"(タイムアウト) — プロンプト数:{len(records)}"
    if r.returncode != 0:
        return f"(claude CLI err{r.returncode}: {r.stderr[:200]}) — プロンプト数:{len(records)}"
    return r.stdout.strip()


def main() -> None:
    SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)
    records = collect()
    today = datetime.now(timezone.utc).date()
    current_week = iso_week(datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc))

    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        try:
            dt = datetime.fromisoformat(r["ts"].replace("Z","+00:00"))
        except Exception:
            continue
        buckets[iso_week(dt)].append(r)

    todo = [(w, recs) for w, recs in buckets.items()
            if w < current_week and len(recs) >= 3 and not (SUMMARIES_DIR / f"{w}.md").exists()]
    todo.sort()

    sys.stderr.write(f"要約対象週: {len(todo)}\n")
    for w, recs in todo:
        sys.stderr.write(f"  {w} ({len(recs)}件) 要約中…\n")
        text = summarize_week(w, recs)
        (SUMMARIES_DIR / f"{w}.md").write_text(text, encoding="utf-8")
        sys.stderr.write(f"  → {w}.md 保存({len(text)}文字)\n")

    sys.stderr.write("完了\n")


if __name__ == "__main__":
    main()
