"""レポートを図入りの PDF にする

1 ページ目に図解(主な数字・流れ・比較・時間ごとの話題)、2 ページ目以降にレポート本文を置いた HTML を作り、
Edge / Chrome のヘッドレス印刷で PDF にする。外部のファイルは読み込まないので、オフラインでも作れる。
"""

import html
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import markdown

VIZ_SCHEMA = {
    "type": "object",
    "properties": {
        "one_line": {"type": "string", "description": "全体を一文で(60字以内)"},
        "key_numbers": {"type": "array", "description": "要点を表す数値・件数 3〜5 個(確かなものだけ)", "items": {
            "type": "object",
            "properties": {"value": {"type": "string"}, "label": {"type": "string"}},
            "required": ["value", "label"], "additionalProperties": False}},
        "flow": {"type": "array", "description": "内容の流れ 3〜6 段階", "items": {
            "type": "object",
            "properties": {"step": {"type": "string"}, "desc": {"type": "string"}},
            "required": ["step", "desc"], "additionalProperties": False}},
        "bars": {"type": "object", "description": "棒グラフにできる比較。なければ items を空に", "properties": {
            "title": {"type": "string"}, "unit": {"type": "string"},
            "items": {"type": "array", "items": {
                "type": "object",
                "properties": {"label": {"type": "string"}, "value": {"type": "number"}},
                "required": ["label", "value"], "additionalProperties": False}}},
            "required": ["title", "unit", "items"], "additionalProperties": False},
    },
    "required": ["one_line", "key_numbers", "flow", "bars"],
    "additionalProperties": False,
}

VIZ_SYSTEM = """レポートから、1 ページの図解に載せる材料を抜き出します。
- レポートに書かれている数値・事実だけを使う。作らない。
- 「聞き取り不確実」「要確認」「推測」と書かれた数値や、候補が複数ある数値は key_numbers・bars に入れない。
- key_numbers の value は短く(例: "0 件", "+57.5pt", "11月20日")。label は 20 字以内。
  会議なら決定事項・ToDo の件数や期限など、要点が伝わる数を選ぶ。
- flow の step は 10 字以内、desc は 40 字以内。研究発表なら手法の流れ、講演なら話の流れ、会議なら議題の流れ。
- bars は同じ単位で比べられる数値が 3 個以上あるときだけ。
- 出力は JSON のみ。"""

# 場面ごとの図の見出し
LABELS = {
    "research": {"numbers": "主な数字", "flow": "手法の流れ"},
    "lecture": {"numbers": "主な数字", "flow": "話の流れ"},
    "meeting": {"numbers": "要点", "flow": "議題の流れ"},
}

CSS = """
@page { size: A4; margin: 16mm 14mm; }
body { font-family: "Yu Gothic UI", "Meiryo", "Hiragino Sans", "Noto Sans CJK JP", sans-serif;
       color: #1d2330; font-size: 10.5pt; line-height: 1.65; }
.cover { border-bottom: 3px solid #2f6fdf; padding-bottom: 8px; margin-bottom: 14px; }
.cover h1 { font-size: 19pt; margin: 0 0 4px; line-height: 1.3; }
.cover .one { font-size: 12pt; color: #2f6fdf; font-weight: 600; }
h2.sec { font-size: 12pt; border-left: 5px solid #2f6fdf; padding-left: 8px; margin: 18px 0 8px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 8px; }
.card { background: #f1f5fd; border-radius: 8px; padding: 10px; text-align: center; }
.card .v { font-size: 17pt; font-weight: 700; color: #2f6fdf; line-height: 1.2; }
.card .l { font-size: 8.5pt; color: #4a5263; margin-top: 4px; }
.flow { display: flex; align-items: stretch; gap: 4px; }
.step { flex: 1; border: 1px solid #c9d6f2; border-radius: 8px; padding: 8px 6px; text-align: center; }
.step .n { display: inline-block; width: 20px; height: 20px; border-radius: 50%; background: #2f6fdf;
           color: #fff; font-size: 9pt; line-height: 20px; }
.step .s { font-weight: 700; margin: 4px 0 2px; font-size: 10pt; }
.step .d { font-size: 8.5pt; color: #4a5263; line-height: 1.45; }
.arrow { align-self: center; color: #2f6fdf; font-weight: 700; }
h3.chart { font-size: 10.5pt; margin: 4px 0; }
.bl { font-size: 11px; fill: #1d2330; } .bv { font-size: 11px; fill: #2f6fdf; font-weight: 700; } .bar { fill: #2f6fdf; }
.tl { display: grid; grid-template-columns: auto 1fr; gap: 3px 10px; font-size: 9.5pt; }
.tl .t { color: #2f6fdf; font-variant-numeric: tabular-nums; font-weight: 600; }
.viz { page-break-after: always; }
#body h1 { display: none; }
#body h2 { font-size: 13pt; border-bottom: 1px solid #c9d6f2; padding-bottom: 2px; margin-top: 20px; page-break-after: avoid; }
#body h3 { page-break-after: avoid; }
#body table { border-collapse: collapse; width: 100%; font-size: 9pt; margin: 6px 0; page-break-inside: avoid; }
#body th, #body td { border: 1px solid #c9d6f2; padding: 3px 6px; vertical-align: top; }
#body th { background: #f1f5fd; }
#body blockquote { border-left: 4px solid #2f6fdf; margin: 8px 0; padding: 4px 10px; background: #f1f5fd; }
#body code { background: #f1f5fd; padding: 0 3px; border-radius: 3px; }
"""

esc = html.escape


def _cards(items):
    return "".join(f'<div class="card"><div class="v">{esc(i["value"])}</div>'
                   f'<div class="l">{esc(i["label"])}</div></div>' for i in items)


def _flow(items):
    return '<div class="arrow">→</div>'.join(
        f'<div class="step"><div class="n">{n}</div><div class="s">{esc(i["step"])}</div>'
        f'<div class="d">{esc(i["desc"])}</div></div>' for n, i in enumerate(items, 1))


def _bars(b):
    items = b.get("items", [])
    if len(items) < 3:
        return ""
    w, row, left = 640, 30, 230
    vmax = max(abs(i["value"]) for i in items) or 1
    svg = [f'<svg viewBox="0 0 {w} {row * len(items) + 10}" width="100%">']
    for k, i in enumerate(items):
        y, bw = k * row + 5, (w - left - 70) * abs(i["value"]) / vmax
        svg.append(f'<text x="{left - 8}" y="{y + 17}" text-anchor="end" class="bl">{esc(i["label"])}</text>'
                   f'<rect x="{left}" y="{y + 4}" width="{max(bw, 2):.1f}" height="18" rx="3" class="bar"/>'
                   f'<text x="{left + bw + 6:.1f}" y="{y + 17}" class="bv">{i["value"]:g}{esc(b.get("unit", ""))}</text>')
    svg.append("</svg>")
    return f'<h2 class="sec">比較</h2><h3 class="chart">{esc(b.get("title", ""))}</h3>' + "".join(svg)


def _timeline(timeline, limit=18):
    """ライブ要約の履歴から、見出しが変わった時刻だけを並べる(同じ見出しが続く分はまとめる)"""
    segs = []
    for t in timeline or []:
        topic = (t.get("topic") or "").strip()
        if topic and (not segs or segs[-1][1] != topic):
            segs.append((t.get("time", ""), topic))
    if len(segs) < 2:
        return ""
    if len(segs) > limit:  # 多すぎるときは間引く(最初と最後は残す)
        step = (len(segs) - 1) / (limit - 1)
        segs = [segs[round(k * step)] for k in range(limit)]
    rows = "".join(f'<div class="t">{esc(time[:5])}</div><div>{esc(topic)}</div>' for time, topic in segs)
    return f'<h2 class="sec">時間ごとの話題</h2><div class="tl">{rows}</div>'


def build_html(report_md: str, viz: dict, timeline: list, mode: str) -> str:
    lab = LABELS.get(mode, LABELS["research"])
    title = next((l[2:].strip() for l in report_md.splitlines() if l.startswith("# ")), "レポート")
    body = markdown.markdown(report_md, extensions=["tables", "fenced_code", "sane_lists"])
    viz_html = (
        f'<div class="cover"><h1>{esc(title)}</h1><div class="one">{esc(viz.get("one_line", ""))}</div></div>'
        + (f'<h2 class="sec">{lab["numbers"]}</h2><div class="cards">{_cards(viz["key_numbers"])}</div>'
           if viz.get("key_numbers") else "")
        + (f'<h2 class="sec">{lab["flow"]}</h2><div class="flow">{_flow(viz["flow"])}</div>' if viz.get("flow") else "")
        + _bars(viz.get("bars") or {})
        + _timeline(timeline)
    )
    return (f'<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>{esc(title)}</title>'
            f"<style>{CSS}</style></head><body><div class=\"viz\">{viz_html}</div>"
            f'<div id="body">{body}</div></body></html>')


def find_browser() -> str | None:
    """PDF 印刷に使う Edge / Chrome / Chromium を探す"""
    candidates = [shutil.which(n) for n in ("msedge", "microsoft-edge", "google-chrome", "chromium", "chromium-browser", "chrome")]
    if sys.platform == "win32":
        candidates += [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                       r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                       r"C:\Program Files\Google\Chrome\Application\chrome.exe"]
    elif sys.platform == "darwin":
        candidates += ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                       "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    return next((c for c in candidates if c and Path(c).exists()), None)


def html_to_pdf(html_text: str, pdf_path: Path) -> None:
    browser = find_browser()
    if not browser:
        raise RuntimeError("PDF にするための Edge / Chrome が見つかりません")
    # 使用中のブラウザと設定が衝突しないよう、使い捨てのプロファイルで印刷する
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "report.html"
        src.write_text(html_text, encoding="utf-8")
        subprocess.run([browser, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                        f"--user-data-dir={Path(tmp) / 'profile'}", f"--print-to-pdf={pdf_path}", src.as_uri()],
                       check=True, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not pdf_path.exists():
        raise RuntimeError("PDF を作れませんでした")


def make_pdf(llm, report_md: str, timeline: list, mode: str, pdf_path: Path) -> None:
    """図の材料を LLM で抜き出し、図入りの PDF を作る"""
    if not find_browser():  # LLM を呼ぶ前に確かめる(むだな費用を出さない)
        raise RuntimeError("PDF にするための Edge / Chrome が見つかりません")
    viz = llm.summary_json(VIZ_SYSTEM, report_md, VIZ_SCHEMA)
    html_to_pdf(build_html(report_md, viz, timeline, mode), pdf_path)
