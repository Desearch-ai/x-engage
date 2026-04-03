#!/usr/bin/env python3
"""
x-engage: Engagement Analyzer + Discord Reporter
Reads x-monitor tweets_window.json (24h sliding window), scores posts,
runs GPT-4o-mini analysis on top-3 performers, generates content ideas
for @desearch_ai, and posts a digest to Discord #x-alerts.

Usage:
    python3 analyze.py              # Full run: analyze + post to Discord
    python3 analyze.py --dry-run    # Print JSON to stdout, no Discord post
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from openai import OpenAI

# ─────────────────────────────────────────────
# Config & Env
# ─────────────────────────────────────────────

load_dotenv()

CONFIG_PATH = os.environ.get("X_ENGAGE_CONFIG", Path(__file__).parent / "config.json")


def _get_discord_token() -> str:
    """
    Load Discord bot token with fallback chain:
    1. DISCORD_BOT_TOKEN env var (or .env file via dotenv)
    2. ~/.openclaw/openclaw.json (same source as post-to-discord.cjs)
    """
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if token:
        return token
    try:
        openclaw_cfg_path = Path.home() / ".openclaw" / "openclaw.json"
        cfg = json.loads(openclaw_cfg_path.read_text())
        token = cfg.get("channels", {}).get("discord", {}).get("token", "")
        if token:
            print("[discord] Token loaded from ~/.openclaw/openclaw.json", file=sys.stderr)
    except Exception as e:
        print(f"[discord] Could not read openclaw.json: {e}", file=sys.stderr)
    return token


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _get_active_account(cfg: dict) -> tuple[str, str]:
    """
    Return (account_id, account_label) for the currently active X account.
    Multi-account: cfg.x_accounts is a list; cfg.active_account selects which one.
    """
    active_id = cfg.get("active_account", "personal")
    accounts = cfg.get("x_accounts", [])
    for acct in accounts:
        if acct.get("id") == active_id:
            return acct["id"], acct.get("label", f"@{active_id}")
    # Fallback: first account or built-in default
    if accounts:
        return accounts[0]["id"], accounts[0].get("label", "@cosmicquantum (personal)")
    return "personal", "@cosmicquantum (personal)"


# ─────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────

def score_tweet(tweet: dict, weights: dict) -> float:
    """
    score = likes*3 + retweets*5 + replies*2 + views*0.01 + quotes*4 + bookmarks*2
    All fields default to 0.0 if None or missing.
    """
    def _val(key: str) -> float:
        v = tweet.get(key)
        return float(v) if v is not None else 0.0

    return (
        _val("like_count") * weights.get("likes", 3)
        + _val("retweet_count") * weights.get("retweets", 5)
        + _val("reply_count") * weights.get("replies", 2)
        + _val("view_count") * weights.get("views", 0.01)
        + _val("quote_count") * weights.get("quotes", 4)
        + _val("bookmark_count") * weights.get("bookmarks", 2)
    )


def get_top_tweets(tweets: list[dict], weights: dict, top_n: int) -> list[dict]:
    scored = []
    for t in tweets:
        s = score_tweet(t, weights)
        scored.append({**t, "_score": round(s, 2)})
    scored.sort(key=lambda x: x["_score"], reverse=True)
    # Deduplicate by tweet id
    seen: set[str] = set()
    deduped: list[dict] = []
    for t in scored:
        tid = t.get("id")
        if tid not in seen:
            seen.add(tid)
            deduped.append(t)
    return deduped[:top_n]


# ─────────────────────────────────────────────
# LLM Analysis
# ─────────────────────────────────────────────

ANALYSIS_SYSTEM = """\
You are an expert social media analyst specialising in X/Twitter performance and tech/AI content.
Analyse the given tweet and respond ONLY with a valid JSON object — no markdown, no prose.
"""

ANALYSIS_USER_TEMPLATE = """\
Analyse this tweet for engagement patterns:

Author: @{username}
Text: {text}
Engagement: {likes} likes | {rts} retweets | {replies} replies | {views} views | {quotes} quotes | {bookmarks} bookmarks
Category: {category}
Engagement Score: {score}

Return exactly this JSON shape (all fields required):
{{
  "hook_type": "<question|data|story|controversy|announcement|list|other>",
  "format": "<single_tweet|thread|media|quote_tweet|other>",
  "emotional_trigger": "<FOMO|curiosity|identity|social_proof|humor|inspiration|fear|other>",
  "why_it_performed": "<1-2 sentence explanation of why this post did well>",
  "audience_fit_score": <1-10 integer>,
  "key_elements": ["<element1>", "<element2>"]
}}
"""

CONTENT_IDEAS_SYSTEM = """\
You are a content strategist for @desearch_ai, an AI-powered search & scraping API on the Bittensor SN22 subnet.
Based on the patterns in the top-performing tweets provided, generate 3 concrete content ideas.
Respond ONLY with a valid JSON array of 3 objects — no markdown, no prose.
"""

CONTENT_IDEAS_USER_TEMPLATE = """\
Here are the top-performing tweet patterns observed:

{patterns_summary}

Generate 3 content ideas for @desearch_ai that leverage these patterns.
Each idea should be specific, actionable, and tailored to the Desearch brand (AI search API, Bittensor SN22, developer audience).

Return exactly this JSON shape:
[
  {{
    "title": "<catchy tweet opener / hook>",
    "format": "<single_tweet|thread|media|announcement>",
    "hook_type": "<question|data|story|controversy|announcement|list>",
    "angle": "<1-2 sentence description of the content angle and why it will perform>",
    "example_opener": "<first 1-2 sentences of the tweet>"
  }},
  ...
]
"""


def _username(tweet: dict) -> str:
    u = tweet.get("user")
    if isinstance(u, dict):
        return u.get("username", "?")
    return "?"


def _strip_markdown_fence(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return raw.strip()


def analyse_tweet_with_llm(client: OpenAI, tweet: dict, model: str) -> dict:
    username = _username(tweet)
    prompt = ANALYSIS_USER_TEMPLATE.format(
        username=username,
        text=tweet.get("text", "")[:500],
        likes=tweet.get("like_count", 0) or 0,
        rts=tweet.get("retweet_count", 0) or 0,
        replies=tweet.get("reply_count", 0) or 0,
        views=tweet.get("view_count", 0) or 0,
        quotes=tweet.get("quote_count", 0) or 0,
        bookmarks=tweet.get("bookmark_count", 0) or 0,
        category=tweet.get("_monitor_category", "unknown"),
        score=tweet.get("_score", 0),
    )
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": ANALYSIS_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        max_tokens=400,
    )
    raw = response.choices[0].message.content or ""
    return json.loads(_strip_markdown_fence(raw))


def generate_content_ideas(client: OpenAI, top_tweets: list[dict], analyses: list[dict], model: str) -> list[dict]:
    patterns = []
    for tweet, analysis in zip(top_tweets[:3], analyses):
        username = _username(tweet)
        patterns.append(
            f"- @{username}: hook={analysis.get('hook_type', '?')}, "
            f"trigger={analysis.get('emotional_trigger', '?')}, "
            f"format={analysis.get('format', '?')}, "
            f"score={tweet.get('_score', 0)}, "
            f"text_snippet=\"{tweet.get('text', '')[:120]}...\""
        )
    patterns_summary = "\n".join(patterns) if patterns else "No pattern data available."

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": CONTENT_IDEAS_SYSTEM},
            {"role": "user", "content": CONTENT_IDEAS_USER_TEMPLATE.format(patterns_summary=patterns_summary)},
        ],
        temperature=0.7,
        max_tokens=700,
    )
    raw = response.choices[0].message.content or ""
    return json.loads(_strip_markdown_fence(raw))


# ─────────────────────────────────────────────
# Discord Formatting
# ─────────────────────────────────────────────

def _fmt_num(n: int | float | None) -> str:
    """Format large numbers compactly: 4200 → 4.2K"""
    if n is None:
        return "0"
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _truncate(text: str, max_len: int = 120) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def build_discord_messages(
    top_10: list[dict],
    analyses: list[dict],
    content_ideas: list[dict],
    now_str: str,
    account_label: str = "@cosmicquantum (personal)",
) -> list[dict]:
    """
    Returns list of Discord API message payloads (content strings).
    Split into multiple messages to stay under Discord's 2000-char limit.
    """
    messages = []

    # ── Message 1: Header + Top 10 list ──────────────────────────────────
    header_lines = [
        f"📊 **Engagement Report** | Top 10 posts • {now_str}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for i, t in enumerate(top_10, 1):
        uname = _username(t)
        score = t.get("_score", 0)
        likes = _fmt_num(t.get("like_count", 0))
        rts = _fmt_num(t.get("retweet_count", 0))
        views = _fmt_num(t.get("view_count", 0))
        text_snip = _truncate(t.get("text", ""), 80)
        header_lines.append(
            f"**#{i}** @{uname} · score **{score:.0f}** · ❤️{likes} 🔄{rts} 👁️{views}"
        )
        header_lines.append(f"> {text_snip}")
    header_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    messages.append({"content": "\n".join(header_lines)})

    # ── Message 2: Deep Dive header ───────────────────────────────────────
    messages.append({"content": "🔍 **Top 3 Deep Dive**"})

    # ── Messages 3-5: Top 3 cards ─────────────────────────────────────────
    hook_emoji = {
        "question": "❓", "data": "📊", "story": "📖",
        "controversy": "🔥", "announcement": "📣", "list": "📋",
    }
    trigger_emoji = {
        "FOMO": "😰", "curiosity": "🤔", "identity": "🪞",
        "social_proof": "👥", "humor": "😂", "inspiration": "✨", "fear": "😱",
    }

    for i, (tweet, analysis) in enumerate(zip(top_10[:3], analyses), 1):
        uname = _username(tweet)
        url = tweet.get("url", "")
        score = tweet.get("_score", 0)
        likes = tweet.get("like_count", 0) or 0
        rts = tweet.get("retweet_count", 0) or 0
        replies = tweet.get("reply_count", 0) or 0
        views = tweet.get("view_count", 0) or 0
        quotes = tweet.get("quote_count", 0) or 0
        bookmarks = tweet.get("bookmark_count", 0) or 0

        hook = analysis.get("hook_type", "?")
        trigger = analysis.get("emotional_trigger", "?")
        fmt = analysis.get("format", "?")
        why = analysis.get("why_it_performed", "")
        fit = analysis.get("audience_fit_score", "?")
        elements = analysis.get("key_elements", [])
        elements_str = " · ".join(elements[:3]) if elements else ""

        card = [
            f"**#{i} @{uname}** · Score **{score:.0f}**",
            f"```{_truncate(tweet.get('text', ''), 200)}```",
            f"❤️ {_fmt_num(likes)}  🔄 {_fmt_num(rts)}  💬 {replies}  "
            f"👁️ {_fmt_num(views)}  📝 {quotes}  🔖 {bookmarks}",
            f"{hook_emoji.get(hook, '📌')} Hook: **{hook}**  "
            f"{trigger_emoji.get(trigger, '💡')} Trigger: **{trigger}**  📐 Format: **{fmt}**",
            f"🎯 Audience Fit: **{fit}/10**",
            f"💡 _{why}_",
        ]
        if elements_str:
            card.append(f"🔑 Key elements: {elements_str}")
        if url:
            card.append(f"🔗 {url}")
        # Account-labelled action buttons
        card.append(
            f"\n**Actions:** 🔄 RT as {account_label}  |  💬 Quote as {account_label}  |  ⏭️ Skip\n"
            f"_(set action in `pending_actions.json`)_"
        )
        messages.append({"content": "\n".join(card)})

    # ── Message 6: Content Ideas ──────────────────────────────────────────
    ideas_lines = ["━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", "💡 **Content Ideas for @desearch_ai**"]
    for j, idea in enumerate(content_ideas, 1):
        title = idea.get("title", "")
        angle = idea.get("angle", "")
        opener = idea.get("example_opener", "")
        fmt = idea.get("format", "")
        ideas_lines.append(
            f"\n**{j}. {title}** _{fmt}_\n"
            f"> {angle}\n"
            f"_Opener:_ \"{opener}\""
        )
    ideas_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    messages.append({"content": "\n".join(ideas_lines)})

    return messages


# ─────────────────────────────────────────────
# Discord API
# ─────────────────────────────────────────────

def post_to_discord(channel_id: str, bot_token: str, messages: list[dict]) -> None:
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json",
    }
    for msg in messages:
        content = msg["content"]
        if len(content) > 2000:
            content = content[:1997] + "…"
        resp = requests.post(url, headers=headers, json={"content": content})
        if not resp.ok:
            print(f"[Discord] Failed: {resp.status_code} {resp.text[:200]}", file=sys.stderr)
        else:
            print(f"[Discord] Posted message (len={len(content)})", file=sys.stderr)


# ─────────────────────────────────────────────
# Pending Actions
# ─────────────────────────────────────────────

def write_pending_actions(
    top_3: list[dict],
    output_path: str,
    account_id: str = "personal",
    account_label: str = "@cosmicquantum (personal)",
) -> None:
    """
    Write top-3 tweets to pending_actions.json for the X Action Executor.
    Status starts as 'pending' — human reviews and sets 'retweet'|'quote'|'skip'.
    account_id + account_label tell the executor which X account to act from.
    """
    now = datetime.now(timezone.utc).isoformat()
    actions = []
    for tweet in top_3:
        actions.append({
            "tweet_id": tweet.get("id", ""),
            "tweet_url": tweet.get("url", ""),
            "tweet_text": tweet.get("text", "")[:280],
            "author": _username(tweet),
            "score": tweet.get("_score", 0),
            "action": "pending",       # human sets: retweet | quote | skip
            "account_id": account_id,
            "account_label": account_label,
            "timestamp": now,
        })

    path = Path(output_path)
    # Merge with existing — only add tweets not already pending
    existing: list[dict] = []
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except Exception:
            existing = []

    existing_ids = {a["tweet_id"] for a in existing}
    new_actions = [a for a in actions if a["tweet_id"] not in existing_ids]
    merged = existing + new_actions

    path.write_text(json.dumps(merged, indent=2, ensure_ascii=False))
    print(f"[pending_actions] Written {len(new_actions)} new entries → {path}", file=sys.stderr)


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def run(dry_run: bool = False) -> dict[str, Any]:
    cfg = load_config()

    # Resolve active X account (multi-account-ready)
    account_id, account_label = _get_active_account(cfg)
    print(f"[analyze] Active account: {account_label} (id={account_id})", file=sys.stderr)

    # Load tweets window
    window_path = Path(cfg["x_monitor_window_path"])
    if not window_path.exists():
        print(f"[warn] tweets_window.json not found at {window_path}, using empty list", file=sys.stderr)
        tweets: list[dict] = []
    else:
        tweets = json.loads(window_path.read_text())

    print(f"[analyze] Loaded {len(tweets)} tweets from window", file=sys.stderr)

    weights = cfg.get("score_weights", {
        "likes": 3, "retweets": 5, "replies": 2,
        "views": 0.01, "quotes": 4, "bookmarks": 2,
    })
    top_n = cfg.get("top_n", 10)
    top_deep = cfg.get("top_deep_dive", 3)
    model = cfg.get("openai_model", "gpt-4o-mini")

    # Score & rank
    top_10 = get_top_tweets(tweets, weights, top_n)
    print(f"[analyze] Top {len(top_10)} tweets selected", file=sys.stderr)

    # LLM: only top-3 get deep-dive (cost-efficient — not all 10)
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if not openai_key:
        raise RuntimeError("OPENAI_API_KEY not set in environment")

    client = OpenAI(api_key=openai_key)
    top_for_deep = top_10[:top_deep]

    analyses: list[dict] = []
    for i, tweet in enumerate(top_for_deep, 1):
        uname = _username(tweet)
        print(f"[llm] Analysing tweet #{i} by @{uname} (score={tweet.get('_score', 0):.1f})", file=sys.stderr)
        analysis = analyse_tweet_with_llm(client, tweet, model)
        analyses.append(analysis)

    # Generate 3 content ideas from detected top-performer patterns
    print("[llm] Generating content ideas…", file=sys.stderr)
    content_ideas = generate_content_ideas(client, top_10, analyses, model)

    # Build result payload
    result: dict[str, Any] = {
        "top_10": [
            {
                "rank": i + 1,
                "id": t.get("id"),
                "url": t.get("url"),
                "author": _username(t),
                "text": t.get("text", "")[:280],
                "score": t.get("_score"),
                "like_count": t.get("like_count", 0) or 0,
                "retweet_count": t.get("retweet_count", 0) or 0,
                "reply_count": t.get("reply_count", 0) or 0,
                "view_count": t.get("view_count", 0) or 0,
                "quote_count": t.get("quote_count", 0) or 0,
                "bookmark_count": t.get("bookmark_count", 0) or 0,
                "category": t.get("_monitor_category", ""),
            }
            for i, t in enumerate(top_10)
        ],
        "analyses": analyses,
        "content_ideas": content_ideas,
        "active_account": {"id": account_id, "label": account_label},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tweet_count_in_window": len(tweets),
    }

    if dry_run:
        # Stdout = pure JSON; all logs were sent to stderr
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return result

    # Write pending actions for human review
    pending_path = cfg.get("pending_actions_path", "pending_actions.json")
    write_pending_actions(
        top_for_deep,
        pending_path,
        account_id=account_id,
        account_label=account_label,
    )

    # Post digest to Discord
    bot_token = _get_discord_token()
    channel_id = str(cfg["discord_channel_id"])
    if not bot_token:
        raise RuntimeError(
            "DISCORD_BOT_TOKEN not set. Set it in .env, environment, or ~/.openclaw/openclaw.json"
        )

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    discord_msgs = build_discord_messages(
        top_10, analyses, content_ideas, now_str, account_label=account_label
    )
    post_to_discord(channel_id, bot_token, discord_msgs)
    print(f"[done] Engagement report posted to Discord #{channel_id}", file=sys.stderr)

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="x-engage: Engagement Analyzer + Discord Reporter")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print analysis JSON to stdout, skip Discord post and pending_actions write",
    )
    args = parser.parse_args()

    try:
        run(dry_run=args.dry_run)
        sys.exit(0)
    except Exception as e:
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)
