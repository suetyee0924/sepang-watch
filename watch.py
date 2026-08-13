#!/usr/bin/env python3
"""
sepang-watch — 2026 雪邦 F1（Bahrain GP in Malaysia）开票与活动监测

职责：抓取 targets.yaml 中的页面/接口，归一化后与上次快照比对，
      有变化或命中关键词时推送告警。不做任何自动下单或验证码绕过。

用法:
    python watch.py                 # 跑一轮
    python watch.py --dry-run       # 只打印，不推送、不写 state
    python watch.py --only f1exp    # 只跑 id 匹配的目标
    python watch.py --init          # 建立初始快照（首次运行用，不告警）
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from email.header import Header
from pathlib import Path
from typing import Any

import feedparser
import httpx
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
STATE_PATH = ROOT / "state" / "snapshots.json"
TARGETS_PATH = ROOT / "targets.yaml"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 sepang-watch/1.0 (personal ticket monitor)"
)

# 页面里每次都会变、但没有意义的东西 —— 不归一化掉的话会天天误报
NOISE_PATTERNS = [
    r"csrf[-_]?token[\"'=:\s]+[\w\-]+",
    r"session[-_]?id[\"'=:\s]+[\w\-]+",
    r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[\.\d]*Z?\b",  # ISO 时间戳
    r"\b\d{10,13}\b",                                       # epoch 时间戳
    r"nonce[\"'=:\s]+[\w\-]+",
    r"__cf_[\w]+=[\w\-\.]+",                                # Cloudflare cookie 残留
    r"\?v=\d+",                                             # 静态资源版本号
    r"build[-_]?id[\"'=:\s]+[\w\-]+",
]


@dataclass
class Target:
    id: str
    url: str
    label: str
    selector: str | None = None
    mode: str = "html"            # html | json | text | rss
    keywords: list[str] = field(default_factory=list)      # 命中 => 高优先级
    negative_keywords: list[str] = field(default_factory=list)  # 存在时说明"还没开"
    filter_keywords: list[str] = field(default_factory=list)    # 仅 rss：条目筛选词
    priority: str = "normal"      # normal | high
    max_chars: int = 20000
    max_entries: int = 40         # 仅 rss：最多看多少条


@dataclass
class Result:
    target: Target
    ok: bool
    changed: bool = False
    hits: list[str] = field(default_factory=list)
    lost: list[str] = field(default_factory=list)
    diff: str = ""
    error: str = ""


def load_targets() -> list[Target]:
    raw = yaml.safe_load(TARGETS_PATH.read_text(encoding="utf-8"))
    return [Target(**t) for t in raw["targets"]]


def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize(text: str) -> str:
    """去掉噪声与多余空白，让 diff 只反映真实内容变化。"""
    for pat in NOISE_PATTERNS:
        text = re.sub(pat, "", text, flags=re.IGNORECASE)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def extract_rss(target: Target, resp: httpx.Response) -> str:
    """RSS/Atom：只保留命中筛选词的条目，按标题+链接输出。

    这样新的相关文章出现 = 内容变化 = 告警；无关文章刷屏不会触发。
    """
    feed = feedparser.parse(resp.content)
    words = [w.lower() for w in (target.filter_keywords or target.keywords)]
    lines: list[str] = []
    for entry in feed.entries[: target.max_entries]:
        blob = " ".join(
            str(entry.get(f, "")) for f in ("title", "summary", "description")
        ).lower()
        if words and not any(w in blob for w in words):
            continue
        lines.append(f"- {entry.get('title', '(无标题)')}\n  {entry.get('link', '')}")
    return "\n".join(lines) if lines else "__NO_MATCHING_ENTRIES__"


def extract(target: Target, resp: httpx.Response) -> str:
    if target.mode == "rss":
        return extract_rss(target, resp)
    if target.mode == "json":
        try:
            return json.dumps(resp.json(), ensure_ascii=False, sort_keys=True, indent=1)
        except Exception:
            return resp.text
    if target.mode == "text":
        return resp.text
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    node = soup.select_one(target.selector) if target.selector else soup
    if node is None:
        # selector 失效本身就值得告警 —— 通常意味着页面改版了
        return "__SELECTOR_NOT_FOUND__\n" + soup.get_text("\n")[: target.max_chars]
    return node.get_text("\n")


def fetch(client: httpx.Client, target: Target) -> Result:
    try:
        resp = client.get(target.url, timeout=30, follow_redirects=True)
    except Exception as exc:
        return Result(target=target, ok=False, error=f"请求失败: {exc}")

    if resp.status_code in (403, 429, 503):
        # Cloudflare / 限流 —— 报告但不当成内容变化，避免误报
        return Result(target=target, ok=False, error=f"被拦截或限流 (HTTP {resp.status_code})")
    if resp.status_code >= 400:
        return Result(target=target, ok=False, error=f"HTTP {resp.status_code}")

    text = normalize(extract(target, resp))[: target.max_chars]
    return Result(target=target, ok=True, diff=text)  # diff 字段先暂存原文


def analyse(result: Result, state: dict[str, Any], init: bool) -> Result:
    t = result.target
    current = result.diff
    lower = current.lower()

    prev = state.get(t.id)

    if prev:
        prev_lower = prev.get("text", "").lower()
        # 只报「新出现」的关键词。像 bahrain / malaysia / f1 这种字样常年挂在
        # 页面上，每轮都判一次「在不在」的话 32 个目标会永远报红，真正开卖那
        # 一下反而淹没在噪音里。rss 的筛选已在 extract 阶段完成，不重复判。
        result.hits = [] if t.mode == "rss" else [
            k for k in t.keywords
            if k.lower() in lower and k.lower() not in prev_lower
        ]
        # 之前有、现在没了的"还没开卖"字样 —— 这是最强的开卖信号
        result.lost = [
            k for k in t.negative_keywords
            if k.lower() in prev_lower and k.lower() not in lower
        ]

    digest = hashlib.sha256(current.encode("utf-8")).hexdigest()

    if init or not prev:
        result.changed = False
    else:
        result.changed = digest != prev.get("sha256")
        if result.changed:
            result.diff = "\n".join(
                list(
                    difflib.unified_diff(
                        prev.get("text", "").splitlines(),
                        current.splitlines(),
                        lineterm="",
                        n=1,
                    )
                )[:60]
            )
        else:
            result.diff = ""

    state[t.id] = {
        "sha256": digest,
        "text": current,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "url": t.url,
    }
    return result


def severity(result: Result) -> str:
    if result.hits or result.lost:
        return "🔴"
    if result.target.priority == "high" and result.changed:
        return "🟠"
    if result.changed:
        return "🟡"
    return "⚪"


def build_message(results: list[Result]) -> str | None:
    alerts = [r for r in results if r.changed or r.hits or r.lost]
    errors = [r for r in results if not r.ok]
    if not alerts and not errors:
        return None

    lines: list[str] = ["*sepang-watch 有动静*", ""]
    for r in sorted(alerts, key=lambda x: (x.target.priority != "high", x.target.id)):
        lines.append(f"{severity(r)} *{r.target.label}*")
        if r.hits:
            lines.append(f"  命中关键词: {', '.join(r.hits)}")
        if r.lost:
            lines.append(f"  消失的字样: {', '.join(r.lost)}  ← 很可能已开卖")
        if r.diff:
            snippet = r.diff[:800]
            lines.append(f"```\n{snippet}\n```")
        lines.append(f"  {r.target.url}")
        lines.append("")

    if errors:
        lines.append("_抓取失败（可能是反爬，非内容变化）_")
        for r in errors:
            lines.append(f"  · {r.target.label}: {r.error}")

    return "\n".join(lines)


def notify_telegram(message: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return False
    for chunk in [message[i : i + 3800] for i in range(0, len(message), 3800)]:
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        if resp.status_code != 200:
            print(f"[warn] Telegram 推送失败: {resp.status_code}", file=sys.stderr)
            return False
    return True


def notify_ntfy(message: str, urgent: bool) -> bool:
    """ntfy.sh —— 无需注册任何账号，装个 App、选个话题名就能收推送。

    话题名相当于密码：任何知道话题名的人都能收到你的推送，也能往里发。
    所以务必用一长串随机字符串，别用 sepang-watch 这种能猜到的名字。
    """
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {
        # HTTP 头只能是 ASCII，直接塞中文会让 httpx 抛 UnicodeEncodeError。
        # ntfy 支持 RFC 2047 编码字，收到后会自己解回「sepang-watch 有动静」。
        "Title": Header("sepang-watch 有动静", "utf-8").encode(),
        "Priority": "urgent" if urgent else "default",
        "Tags": "checkered_flag",
        "Markdown": "yes",
    }
    token = os.environ.get("NTFY_TOKEN")  # 自建服务器或付费账号才需要
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = httpx.post(
        f"{server}/{topic}", content=message.encode("utf-8"), headers=headers, timeout=20
    )
    if resp.status_code >= 300:
        print(f"[warn] ntfy 推送失败: {resp.status_code}", file=sys.stderr)
        return False
    return True


def notify_github_issue(message: str, urgent: bool) -> bool:
    """在自己的仓库开一个 Issue —— GitHub 会自动发邮件 + 手机 App 推送。

    完全不需要第三方服务，GitHub Actions 里 GITHUB_TOKEN 是自动注入的。
    需要在 workflow 里授予 issues: write 权限。

    默认关闭：GITHUB_TOKEN 和 GITHUB_REPOSITORY 在 Actions 里永远有值，
    只靠它们判断会导致每次告警都顺手开一个 Issue。要启用这个通道，
    把仓库变量 USE_GITHUB_ISSUE 设成 true。
    """
    if os.environ.get("USE_GITHUB_ISSUE", "").strip().lower() not in ("1", "true", "yes"):
        return False
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not (token and repo):
        return False
    title = ("🔴 " if urgent else "🟡 ") + "sepang-watch: " + time.strftime(
        "%m-%d %H:%M UTC", time.gmtime()
    )
    resp = httpx.post(
        f"https://api.github.com/repos/{repo}/issues",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        json={"title": title, "body": message[:60000]},
        timeout=20,
    )
    if resp.status_code >= 300:
        print(f"[warn] GitHub Issue 创建失败: {resp.status_code}", file=sys.stderr)
        return False
    return True


def notify_webhook(message: str, urgent: bool = False) -> bool:
    """Discord / Slack 的 Incoming Webhook —— 都不需要手机号注册。"""
    sent = False
    discord = os.environ.get("DISCORD_WEBHOOK_URL")
    if discord:
        body = message
        # 高优先级时 @自己：webhook 消息默认不会触发手机推送，
        # 带上 mention 才能在频道被静音时也弹出来。
        mention = os.environ.get("DISCORD_MENTION_ID")
        if urgent and mention:
            body = f"<@{mention}> ⚠️ 开卖信号\n\n{message}"
        for chunk in [body[i : i + 1900] for i in range(0, len(body), 1900)]:
            r = httpx.post(
                discord,
                json={
                    "content": chunk,
                    "allowed_mentions": {"parse": ["users"]},
                },
                timeout=20,
            )
            if r.status_code >= 300:
                print(f"[warn] Discord 推送失败: {r.status_code}", file=sys.stderr)
            else:
                sent = True
    slack = os.environ.get("SLACK_WEBHOOK_URL")
    if slack:
        r = httpx.post(slack, json={"text": message[:38000]}, timeout=20)
        if r.status_code >= 300:
            print(f"[warn] Slack 推送失败: {r.status_code}", file=sys.stderr)
        else:
            sent = True
    return sent


def notify(message: str, urgent: bool = False) -> None:
    """按配置了哪些环境变量决定走哪些通道，可以同时开多条。

    每条通道单独兜异常：推送失败最多是少收一条告警，绝不能把整轮跑挂掉——
    main() 里 save_state() 在 notify() 之后，这里抛出去会导致快照永远不更新，
    下一轮拿旧快照继续报同一件事，然后继续挂。
    """
    channels = []
    for name, fn in (
        ("ntfy", lambda: notify_ntfy(message, urgent)),
        ("telegram", lambda: notify_telegram(message)),
        ("github-issue", lambda: notify_github_issue(message, urgent)),
        ("webhook", lambda: notify_webhook(message, urgent)),
    ):
        try:
            channels.append(fn())
        except Exception as exc:
            print(f"[warn] {name} 推送异常: {exc!r}", file=sys.stderr)
            channels.append(False)
    if not any(channels):
        print(
            "[warn] 没有可用的推送通道 —— 请至少配置 NTFY_TOPIC、TELEGRAM_BOT_TOKEN、"
            "GITHUB_TOKEN 或 DISCORD_WEBHOOK_URL 其中之一",
            file=sys.stderr,
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--init", action="store_true", help="建立初始快照，不告警")
    ap.add_argument("--only", help="只跑 id 包含该字符串的目标")
    args = ap.parse_args()

    targets = load_targets()
    if args.only:
        targets = [t for t in targets if args.only in t.id]

    state = load_state()
    results: list[Result] = []

    headers = {
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,ja;q=0.7",
    }
    with httpx.Client(headers=headers) as client:
        for t in targets:
            r = fetch(client, t)
            if r.ok:
                r = analyse(r, state, args.init)
            results.append(r)
            print(f"{severity(r)} {t.id:<24} {t.label}"
                  + (f"  [{r.error}]" if r.error else ""))
            time.sleep(2.5)  # 礼貌间隔，别把人家站点打疼

    message = build_message(results)
    # 抓取失败本身不值得半夜推送 —— 反爬和站点抽风是常态，真正要叫醒你的
    # 只有内容变化 / 关键词新出现 / 「还没开卖」字样消失这三种。
    alerting = any(r.changed or r.hits or r.lost for r in results)
    if args.init:
        print("\n[init] 初始快照已建立，本轮不告警。")
    elif message:
        print("\n" + message)
        if not args.dry_run and alerting:
            urgent = any(r.hits or r.lost for r in results)
            notify(message, urgent=urgent)
        elif not alerting:
            print("\n[info] 只有抓取失败、无内容变化 —— 不推送。")
    else:
        print("\n无变化。")

    if not args.dry_run:
        save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
