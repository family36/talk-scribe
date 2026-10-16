"""talk-scribe: 発表・講演・会議を聞きながらライブ要約し、最後に Markdown のレポート(議事録)を作るサーバー

使い方:
    python server.py               # Edge のアプリウィンドウで http://localhost:8765 を開く
    python server.py --no-browser  # ブラウザを開かない
LLM の設定は .env に書く(.env.example を参照)。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from llm import describe_error, load_dotenv, make_llm, strip_think
from pdf import make_pdf

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
PORT = int(os.environ.get("PORT", "8765"))
REPORTS_DIR = ROOT / "reports"
LOG_DIR = ROOT / "logs"  # 音声認識のイベントログ(logs/speech.log)

_llms: dict = {}


def get_llm(role: str):
    """role は "summary"(ライブ要約)か "report"(レポート)。初回に .env の設定から作る"""
    if role not in _llms:
        _llms[role] = make_llm(role)
    return _llms[role]


SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "now": {"type": "string", "description": "new_transcript で新しく話された内容だけの要約(1〜2文、内容を直接書く)"},
        "topic": {"type": "string", "description": "現在のトピックを表す短い見出し(15字程度)"},
        "terms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"term": {"type": "string"}, "meaning": {"type": "string"}},
                "required": ["term", "meaning"],
                "additionalProperties": False,
            },
            "description": "new_transcript に出てきた専門用語・略語とその簡潔な説明(最大3つ)",
        },
    },
    "required": ["now", "topic", "terms"],
    "additionalProperties": False,
}

# ---------------------------------------------------------------- 場面ごとの指示
# 画面の「場面」(研究発表 / 講演・セミナー / 会議)に合わせて、AI への指示を切り替える
MODES = {
    "research": {
        "scene": "研究発表を聴講している人",
        "unit": "発表",
        "doc": "聴講レポート",
        "keep": "数値・手法名・データセット名",
        "structure": """# <発表タイトル>
- 発表者 / セッション・会場 / 日時 などのメタ情報(分かる範囲)
## TL;DR(3行以内)
## 背景・課題
## 提案手法
## 実験・結果(数値は可能な限り具体的に)
## 結論・今後の課題
## 質疑応答(あれば)
## 所感・自分の研究との関連(メモをもとに。なければ論点の提案)
## 気になった点・調べること
## キーワード""",
    },
    "lecture": {
        "scene": "講演・セミナーを聴講している人",
        "unit": "講演",
        "doc": "聴講レポート",
        "keep": "固有名詞・数値・引用された言葉",
        "structure": """# <講演タイトル>
- 講演者 / イベント・会場 / 日時 などのメタ情報(分かる範囲)
## TL;DR(3行以内)
## 講演の流れ(主な話題を順に)
## 主な主張・ポイント
## 具体例・エピソード・データ
## 印象に残った言葉(できるだけ原文のまま)
## 質疑応答(あれば)
## 所感(メモをもとに。なければ考えるための問いの提案)
## 調べたいこと・次のアクション
## キーワード""",
    },
    "meeting": {
        "scene": "会議に参加している人",
        "unit": "会議",
        "doc": "議事録",
        "keep": "決定事項・依頼・担当者・期限・数値",
        "structure": """# <会議名>
- 日時・場所 / 参加者 などのメタ情報(分かる範囲)
## 要約(3行以内)
## 議題と議論の内容(議題ごとに)
## 決定事項
## ToDo(担当・期限が分かれば併記)
## 保留・持ち越し事項
## 次回の予定(あれば)
## 補足メモ(メモをもとに)""",
    },
}

SUMMARY_SYSTEM = """あなたは{scene}を支援するアシスタントです。
{unit}の音声認識テキスト(誤認識を含む)を受け取り、今の話についていけるようリアルタイム要約を作ります。
- 出力言語: {lang}
- 音声認識の誤りは文脈から推測して補正する。{keep}は正確に残す。
- "now" は new_transcript(前回の要約以降に新しく話された部分)だけを要約する。
  context_transcript と previous_summary は前回までに要約済みの部分で、つながりを理解するためだけに使う。
  previous_summary に書いたことは繰り返さない。同じ話題が続いているなら、新しく加わった情報(詳細・数値・例)だけを書く。
- 話された内容そのものを簡潔に書く。
  「〜を説明している」「〜し始めている」「〜と述べた」「〜について話している」のような
  様子の実況・描写はしない。主張・内容・数値をそのまま書く。
  悪い例: 「新しい手法の説明を始めている。」
  良い例: 「新手法は計算量を二次から線形に下げ、精度は従来より 0.5 ポイント高い。」
- 話されていない情報を作らない。
- 出力は次の形の JSON だけにする(前置き・コードフェンスは不要):
  {{"now": "要約", "topic": "短い見出し", "terms": [{{"term": "用語", "meaning": "説明"}}]}}"""

REPORT_SYSTEM = """あなたは{scene}の{doc}作成を手伝うアシスタントです。
{unit}の文字起こし(音声認識なので誤りを含む)、ライブ要約の履歴、本人のメモから、
後で読み返して役立つ{doc}を Markdown で作成します。

- 出力言語: {lang}(固有名詞・専門用語は原語を括弧で併記)
- 音声認識の誤りは文脈から補正するが、確信が持てない箇所は「(聞き取り不確実)」と明記する。
- 文字起こしにない内容を創作しない。一般知識で補足する場合は「補足:」と明示する。
- 本人のメモは重要なので必ず反映し、メモ由来であることが分かるようにする。
- 該当する内容がない節は「(なし)」と書くか省く。
- Markdown 本文のみを出力する(前置きや ```markdown フェンスは不要)。

構成:
{structure}"""


def system_prompt(template: str, payload: dict) -> str:
    """場面(payload["mode"])と出力言語を埋めた指示文を作る"""
    mode = MODES.get(payload.get("mode"), MODES["research"])  # 不明・旧い値は研究発表
    return template.format(lang=payload.get("lang") or "日本語", **mode)


def live_summary(payload: dict) -> dict:
    new = payload.get("new", "").strip()
    if not new:
        raise ValueError("文字起こしが空です")
    parts = []
    if payload.get("title"):
        parts.append(f"<title>{payload['title']}</title>")
    if payload.get("context"):
        parts.append(f"<context_transcript>\n{payload['context']}\n</context_transcript>")
    if payload.get("previous_now"):
        parts.append(f"<previous_summary>\n{payload['previous_now']}\n</previous_summary>")
    parts.append(f"<new_transcript>\n{new}\n</new_transcript>")
    parts.append("new_transcript で新しく話された内容だけを要約してください。")

    return get_llm("summary").summary_json(system_prompt(SUMMARY_SYSTEM, payload), "\n\n".join(parts), SUMMARY_SCHEMA)


def build_report_prompt(payload: dict) -> str:
    meta = payload.get("meta", {})
    meta_lines = "\n".join(f"{k}: {v}" for k, v in meta.items() if v)
    timeline = "\n".join(
        f"[{t.get('time', '')}] {t.get('topic', '')}: {t.get('now', '')}"
        for t in payload.get("timeline", [])
    )
    return f"""<meta>
{meta_lines or "(未入力)"}
</meta>

<my_notes>
{payload.get("notes", "").strip() or "(なし)"}
</my_notes>

<live_summary_timeline>
{timeline or "(なし)"}
</live_summary_timeline>

<transcript>
{payload.get("transcript", "").strip() or "(なし)"}
</transcript>

以上をもとに作成してください。"""


ASK_SYSTEM = """あなたは{scene}を助けるアシスタントです。
今聞いている{unit}について質問されます。次の材料をもとに答えてください。
- live_summary_timeline: {unit}の最初から今までの流れ(数十秒ごとの要約。古い部分は見出しだけ)
- recent_transcript: 直近数分の文字起こし(音声認識なので誤りを含む)。細かい数値や言い回しはこちらを優先する
- my_notes: 本人のメモ
- 出力言語: {lang}(固有名詞・専門用語は原語を併記してよい)
- {unit}で話された内容を根拠にして、簡潔に答える(長くても 10 行程度)。
- 話されていないことを一般知識で補うときは「補足:」と明示する。
- 材料から分からないことは、分からないとはっきり書く(要約に残っていない細部は分からない扱い)。推測で埋めない。
- Markdown の見出しは使わない。箇条書きは使ってよい。"""


def build_ask_prompt(payload: dict) -> str:
    # 古い部分は見出し(topic)だけ、新しい部分は要約文まで届く
    timeline = "\n".join(f"[{t.get('time', '')}] {t.get('topic', '')}" + (f": {t['now']}" if t.get("now") else "")
                         for t in payload.get("timeline", []))
    return f"""<title>{payload.get("title") or "(未入力)"}</title>

<live_summary_timeline>
{timeline or "(なし)"}
</live_summary_timeline>

<my_notes>
{payload.get("notes", "").strip() or "(なし)"}
</my_notes>

<recent_transcript>
{payload.get("transcript", "").strip() or "(まだありません)"}
</recent_transcript>

<question>
{payload["question"].strip()}
</question>"""


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip(" ._")
    return name[:60] or "report"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # 本文終了 = 接続クローズ(ストリーミングを単純化)

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), fmt % args))

    def _json(self, code: int, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/jobs":
            with JOBS_LOCK:
                jobs = [{k: v for k, v in j.items() if k != "text"} for j in JOBS.values()]
            self._json(200, jobs[::-1])
        elif self.path.startswith("/api/jobs/"):
            job = JOBS.get(unquote(self.path.rsplit("/", 1)[1]))
            self._json(200, job) if job else self._json(404, {"error": "not found"})
        else:
            self.send_error(404)

    def do_POST(self):
        try:
            payload = self._read_json()
        except ValueError:  # JSONDecodeError / UnicodeDecodeError
            return self._json(400, {"error": "invalid JSON"})

        if self.path == "/api/summary":
            try:
                self._json(200, live_summary(payload))
            except Exception as e:  # noqa: BLE001  プロバイダごとの例外をまとめて扱う
                code, message = describe_error(e)
                self._json(code, {"error": message})

        elif self.path == "/api/log":  # ブラウザ側の音声認識イベントを記録(原因調査用)
            line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] speech {json.dumps(payload, ensure_ascii=False)}\n"
            sys.stderr.write(line)
            LOG_DIR.mkdir(exist_ok=True)
            with open(LOG_DIR / "speech.log", "a", encoding="utf-8") as f:
                f.write(line)
            self._json(200, {})
        elif self.path == "/api/open-reports":  # 保存先フォルダをエクスプローラー等で開く
            # 画面でレポートを選んでいればそのフォルダ、なければ reports/ を開く
            REPORTS_DIR.mkdir(exist_ok=True)
            target = REPORTS_DIR
            if payload.get("id"):
                folder = (REPORTS_DIR / str(payload["id"])).resolve()
                if folder.parent == REPORTS_DIR.resolve() and folder.is_dir():  # reports/ の外は開かない
                    target = folder
            open_folder(target)
            self._json(200, {"opened": str(target.relative_to(ROOT))})
        elif self.path == "/api/ask":
            self._stream_answer(payload)
        elif self.path == "/api/report":
            self._json(200, {"id": start_report_job(payload)})
        else:
            self.send_error(404)

    def _stream_answer(self, payload: dict):
        """「AIに聞く」の答えを少しずつ返す。最初の一片が届く前のエラーは JSON で返す"""
        if not payload.get("question", "").strip():
            return self._json(400, {"error": "質問が空です"})
        chunks = strip_think(get_llm("ask").stream_text(system_prompt(ASK_SYSTEM, payload), build_ask_prompt(payload), "ask"))
        try:
            first = next(chunks, "")
        except Exception as e:  # noqa: BLE001
            code, message = describe_error(e)
            return self._json(code, {"error": message})
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(first.encode("utf-8"))
            for text in chunks:
                self.wfile.write(text.encode("utf-8"))
                self.wfile.flush()
        except ConnectionError:  # 画面側が閉じられた
            pass
        except Exception as e:  # noqa: BLE001
            self.wfile.write(f"\n\n(途中でエラー: {describe_error(e)[1]})".encode("utf-8"))


# ---------------------------------------------------------------- レポートのバックグラウンド作成
# 作成中に次の発表を聞き始められるよう、レポートはサーバー側のスレッドで作る
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def start_report_job(payload: dict) -> str:
    # title は画面が送る。古い依頼(再作成)は meta の「タイトル」にしか入っていない
    title = payload.get("title") or payload.get("meta", {}).get("タイトル") or "report"
    base = f"{datetime.now():%Y%m%d_%H%M%S}_{safe_filename(title)}"
    # 1 件ごとにフォルダを作り、レポートと文字起こしを並べて保存する(reports/<日時_タイトル>/)
    # files: 保存したファイル名、stage: 作成中の段階(画面に出す)、note: 失敗はしないが知らせたいこと
    job = {"id": base, "title": title, "files": [], "status": "running", "stage": "",
           "started": datetime.now().strftime("%H:%M"), "text": "", "error": "", "note": ""}
    with JOBS_LOCK:
        JOBS[base] = job
    threading.Thread(target=run_report_job, args=(job, payload), daemon=True).start()
    return base


def run_report_job(job: dict, payload: dict) -> None:
    folder = REPORTS_DIR / job["id"]
    folder.mkdir(parents=True, exist_ok=True)
    # 文字起こしは先に保存(レポートが失敗しても残るように)
    (folder / "transcript.txt").write_text(payload.get("transcript", ""), encoding="utf-8")
    fmt = payload.get("format") if payload.get("format") in ("md", "pdf", "both") else "md"
    try:
        job["stage"] = "文章を作成中"
        for text in get_llm("report").stream_text(system_prompt(REPORT_SYSTEM, payload), build_report_prompt(payload), "report"):
            job["text"] += text
        # 推論過程を <think> で出すローカルモデル向けに、保存前に取り除く
        job["text"] = re.sub(r"<think>[\s\S]*?</think>\s*", "", job["text"])
        if fmt in ("md", "both"):
            save_report_file(job, folder / "report.md", lambda p: p.write_text(job["text"], encoding="utf-8"))
        if fmt in ("pdf", "both"):
            job["stage"] = "PDF を作成中"
            try:
                save_report_file(job, folder / "report.pdf", lambda p: make_pdf(
                    get_llm("summary"), job["text"], payload.get("timeline", []), payload.get("mode"), p))
            except Exception as e:  # noqa: BLE001  PDF だけ失敗したら、文章は .md で残す
                job["note"] = f"PDF を作れませんでした({describe_error(e)[1]})"
                sys.stderr.write(f"[{datetime.now():%H:%M:%S}] pdf failed: {e}\n")
                if "report.md" not in job["files"]:
                    save_report_file(job, folder / "report.md", lambda p: p.write_text(job["text"], encoding="utf-8"))
        job["status"], job["stage"] = "done", ""
    except Exception as e:  # noqa: BLE001
        job["status"], job["error"] = "error", describe_error(e)[1]
        sys.stderr.write(f"[{datetime.now():%H:%M:%S}] report failed: {e}\n")


def save_report_file(job: dict, path: Path, write) -> None:
    write(path)
    job["files"].append(path.name)
    sys.stderr.write(f"[{datetime.now():%H:%M:%S}] saved {path.relative_to(ROOT)}\n")


def open_folder(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
        # サーバーは裏で動いているので、Windows はそのままだとエクスプローラーを前面に出さない
        threading.Thread(target=_bring_explorer_to_front, args=(path.name,), daemon=True).start()
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


def _bring_explorer_to_front(folder_name: str, timeout: float = 3.0) -> None:
    """開いたエクスプローラーのウィンドウを探して最前面に出す。見つからなければ何もしない
    タイトルは Windows のバージョンで「フォルダ名」または「フォルダ名 - エクスプローラー」"""
    import ctypes
    import time
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        cls, title = ctypes.create_unicode_buffer(64), ctypes.create_unicode_buffer(512)
        user32.GetClassNameW(hwnd, cls, 64)
        user32.GetWindowTextW(hwnd, title, 512)
        t = title.value
        if (cls.value == "CabinetWClass" and (t == folder_name or t.startswith(folder_name + " - "))
                and user32.IsWindowVisible(hwnd)):
            found.append(hwnd)
            return False
        return True

    end = time.time() + timeout
    while not found and time.time() < end:
        user32.EnumWindows(visit, 0)
        if not found:
            time.sleep(0.1)
    if not found:
        return
    hwnd = found[0]
    # Alt キーを一瞬押したことにすると、裏のプロセスからでも前面に出せる(Windows の前面化制限の回避策)
    VK_MENU, KEYEVENTF_KEYUP, SW_RESTORE = 0x12, 0x2, 9
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)


def open_window(url):
    """Edge のアプリ表示で開く(Edge の音声認識は Chrome より聞き取りが良い)。なければ既定ブラウザ"""
    for exe in (shutil.which("msedge"),
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if exe and Path(exe).exists():
            subprocess.Popen([exe, f"--app={url}", "--window-size=1400,1000"])
            return
    webbrowser.open(url)


def main():
    try:  # 設定の不足は起動時に知らせる
        summary_llm, report_llm = get_llm("summary"), get_llm("report")
    except (ValueError, ImportError) as e:
        print(f"LLM の設定エラー: {e}", file=sys.stderr)
        sys.exit(1)
    # Windows では SO_REUSEADDR だと同じポートで 2 つ起動でき、古い方が応答し続けてしまうので禁止する
    ThreadingHTTPServer.allow_reuse_address = False
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"ポート {PORT} は使用中です。すでに server.py が動いていないか確認してください。", file=sys.stderr)
        sys.exit(1)
    url = f"http://localhost:{PORT}"
    print(f"起動しました: {url}  (summary={summary_llm.name}, report={report_llm.name}, Ctrl+C で終了)")
    if "--no-browser" not in sys.argv:
        open_window(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
