"""LLM プロバイダの切り替え。設定は .env(または環境変数)で行う。

    LLM_PROVIDER     anthropic(既定) / openai / local
    SUMMARY_PROVIDER ライブ要約だけ別のプロバイダにしたいとき(省略時は LLM_PROVIDER)
    REPORT_PROVIDER  レポートだけ別のプロバイダにしたいとき(省略時は LLM_PROVIDER)
    SUMMARY_MODEL    ライブ要約のモデル(anthropic は省略可)
    REPORT_MODEL     レポートのモデル(anthropic は省略可)
    ASK_PROVIDER / ASK_MODEL  「AIに聞く」用(省略時はライブ要約と同じ)

    anthropic: ANTHROPIC_API_KEY
    openai:    OPENAI_API_KEY(OPENAI_BASE_URL で接続先も変えられる)
    local:     LOCAL_BASE_URL(既定 http://localhost:11434/v1 = Ollama)、LOCAL_API_KEY(不要なら省略)
               Ollama・LM Studio・llama.cpp server・vLLM など OpenAI 互換 API を持つサーバー
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterator

PROVIDERS = ("anthropic", "openai", "local")
DEFAULT_MODELS = {  # anthropic だけ既定値を持つ(他は利用者の環境で使えるモデルが違うため必須)
    "anthropic": {"summary": "claude-sonnet-5-5", "report": "claude-opus-5-5"},
}


def load_dotenv(path: Path) -> None:
    """KEY=VALUE 形式の .env を読む。すでに設定されている環境変数は上書きしない"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().removeprefix("export ").strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()  # 行末コメント
        os.environ.setdefault(key, value)


def log_usage(kind: str, model: str, tokens_in, tokens_out) -> None:
    sys.stderr.write(f"[{datetime.now():%H:%M:%S}] {kind} {model} in={tokens_in} out={tokens_out}\n")


def parse_json_loose(text: str) -> dict:
    """JSON スキーマ指定に対応しないモデル向け: 前置き・コードフェンス・<think> を除いて最初の {...} を読む"""
    text = re.sub(r"<think>[\s\S]*?</think>", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            raise ValueError(f"モデルの出力が JSON ではありません: {text[:200]}")
        return json.loads(m.group(0))


def strip_think(chunks: Iterator[str]) -> Iterator[str]:
    """流れてくる文章の先頭の <think>…</think>(推論過程を出すローカルモデル向け)を取り除く"""
    buf, state = "", "undecided"  # undecided → think(中を捨てる)/ pass(そのまま流す)
    for c in chunks:
        if state == "pass":
            yield c
            continue
        buf += c
        if state == "undecided":
            head = buf.lstrip()
            if len(head) < len("<think>") and "<think>".startswith(head):
                continue  # まだ判断できない
            if not head.startswith("<think>"):
                state = "pass"
                yield buf
                continue
            state = "think"
        end = buf.find("</think>")
        if end >= 0:
            state, rest = "pass", buf[end + len("</think>"):].lstrip()
            if rest:
                yield rest
    if state == "undecided" and buf:
        yield buf


def describe_error(e: Exception) -> tuple[int, str]:
    """どのプロバイダの例外でも、画面に出す HTTP ステータスと日本語メッセージにする"""
    name = type(e).__name__
    if name == "RateLimitError":
        return 429, "レート制限中です。少し待ってください。"
    if name in ("APIConnectionError", "APITimeoutError"):
        return 502, "LLM の API に接続できません(ネットワーク・接続先 URL を確認)"
    status = getattr(e, "status_code", None)
    if status is not None:
        return 502, f"API error {status}: {getattr(e, 'message', e)}"
    return 500, str(e)


# ---------------------------------------------------------------- Anthropic (Claude)
class AnthropicLLM:
    # 拒否時にサーバー側で別モデルへ自動フォールバックできるモデル
    FALLBACK_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")
    # effort を受け付けないモデル
    NO_EFFORT_PREFIXES = ("claude-haiku-4-5", "claude-sonnet-4-5", "claude-3")

    def __init__(self, model: str):
        import anthropic

        self.client = anthropic.Anthropic()  # ANTHROPIC_API_KEY を読む
        self.model = model

    def _kwargs(self, effort: str) -> dict:
        kw = {}
        if self.model in self.FALLBACK_MODELS:
            kw.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        if not self.model.startswith(self.NO_EFFORT_PREFIXES):
            kw["output_config"] = {"effort": effort}
        return kw

    def summary_json(self, system: str, user: str, schema: dict) -> dict:
        kw = self._kwargs("low")  # 速度優先
        kw.setdefault("output_config", {})["format"] = {"type": "json_schema", "schema": schema}
        if self.model == "claude-sonnet-5-5":
            kw["thinking"] = {"type": "between_tools"}  # Sonnet 5.5 で思考を切る(ツールなしなので実質思考なし)
        with self.client.beta.messages.stream(
            model=self.model, max_tokens=4000, system=system,
            messages=[{"role": "user", "content": user}], **kw,
        ) as stream:
            msg = stream.get_final_message()
        log_usage("summary", msg.model, msg.usage.input_tokens, msg.usage.output_tokens)
        if msg.stop_reason == "refusal":
            raise RuntimeError("モデルが応答を拒否しました")
        return parse_json_loose("".join(b.text for b in msg.content if b.type == "text"))

    def stream_text(self, system: str, user: str, kind: str = "report") -> Iterator[str]:
        """文章を少しずつ返す。kind: "report"(丁寧・長文)/ "ask"(質問への短い答え)"""
        effort, max_tokens = ("high", 32000) if kind == "report" else ("medium", 4000)
        with self.client.beta.messages.stream(
            model=self.model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}], **self._kwargs(effort),
        ) as stream:
            yield from stream.text_stream
            msg = stream.get_final_message()
        log_usage(kind, msg.model, msg.usage.input_tokens, msg.usage.output_tokens)
        if msg.stop_reason == "refusal":
            yield "\n\n> ⚠ モデルが応答を拒否しました。\n"


# ---------------------------------------------------------------- OpenAI / OpenAI 互換(ローカル LLM)
class OpenAILLM:
    def __init__(self, model: str, base_url: str | None = None, api_key: str | None = None):
        import openai

        self.openai = openai
        self.client = openai.OpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    def summary_json(self, system: str, user: str, schema: dict) -> dict:
        kw = dict(model=self.model, messages=[{"role": "system", "content": system},
                                              {"role": "user", "content": user}])
        try:
            resp = self.client.chat.completions.create(
                **kw, response_format={"type": "json_schema",
                                       "json_schema": {"name": "live_summary", "schema": schema, "strict": True}})
        except self.openai.BadRequestError:
            # JSON スキーマ指定に対応しないサーバー・モデル: 指定なしで頼み、出力から JSON を拾う
            resp = self.client.chat.completions.create(**kw)
        if resp.usage:
            log_usage("summary", resp.model, resp.usage.prompt_tokens, resp.usage.completion_tokens)
        return parse_json_loose(resp.choices[0].message.content or "")

    def stream_text(self, system: str, user: str, kind: str = "report") -> Iterator[str]:
        """文章を少しずつ返す。kind はログの表示用("report" / "ask")"""
        stream = self.client.chat.completions.create(
            model=self.model, stream=True,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
        log_usage(kind, self.model, "-", "-")


# ---------------------------------------------------------------- 設定から作る
def make_llm(role: str):
    """role は "summary" / "report" / "ask"。設定が足りなければ ValueError
    "ask"(AIに聞く)は ASK_PROVIDER / ASK_MODEL がなければライブ要約と同じ設定を使う"""
    roles = [role, "summary"] if role == "ask" else [role]
    env = lambda suffix: next((os.environ[f"{r.upper()}_{suffix}"] for r in roles
                               if os.environ.get(f"{r.upper()}_{suffix}")), None)
    provider = (env("PROVIDER") or os.environ.get("LLM_PROVIDER") or "anthropic").lower()
    if provider not in PROVIDERS:
        raise ValueError(f"{role.upper()}_PROVIDER / LLM_PROVIDER は {', '.join(PROVIDERS)} のどれかにしてください: {provider}")
    model = env("MODEL") or DEFAULT_MODELS.get(provider, {}).get(roles[-1])
    if not model:
        raise ValueError(f"{provider} を使うときは .env に {roles[-1].upper()}_MODEL を書いてください")

    if provider == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            sys.stderr.write("警告: ANTHROPIC_API_KEY が設定されていません\n")
        llm = AnthropicLLM(model)
    elif provider == "openai":
        if not os.environ.get("OPENAI_API_KEY"):
            raise ValueError("openai を使うときは .env に OPENAI_API_KEY を書いてください")
        llm = OpenAILLM(model)
    else:  # local: キー不要のサーバーが多いが、SDK は空のキーを受け付けないのでダミーを渡す
        llm = OpenAILLM(model, base_url=os.environ.get("LOCAL_BASE_URL", "http://localhost:11434/v1"),
                        api_key=os.environ.get("LOCAL_API_KEY") or "local")
    llm.name = f"{provider}:{model}"
    return llm
