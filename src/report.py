"""
Weekly clutter report: gathers unread-sender data, asks an AI provider to
classify it, and formats a summary. This module only reads your mailbox
and (optionally) emails a report to yourself — it never deletes, trashes,
or unsubscribes from anything. Acting on what the report finds is a
separate, explicit step using the existing menu options.
"""

import re
from collections import Counter
from typing import Any, Dict, List, Optional

from ai_provider import get_provider

CLASSIFICATIONS = ("delete_and_unsubscribe", "delete_only", "keep")


def gather_sender_data(gmail, limit: int = 60, days: int = 7) -> List[Dict[str, Any]]:
    """Build the per-sender dataset the AI classifies: unread counts plus
    a read-only List-Unsubscribe support check (never fires anything —
    see check_unsubscribe_support in methods.py).

    Scoped to the last `days` days by design: a *weekly* report should
    reflect what's piled up recently, not force a full scan of your entire
    historical unread backlog every time it runs — that's slow and beside
    the point for something meant to run on a schedule. Always runs its
    own fresh scan rather than reusing gmail.users, since that attribute
    may already hold a different, unscoped result if option 1 ran earlier
    in the same session.
    """
    gmail.users = []
    gmail.total_messages = 0
    messages, _ = gmail.list_messages("me", query=f"is:unread newer_than:{days}d")
    gmail.batch_get(messages)

    counts = Counter(gmail.users).most_common(limit)

    senders = []
    for raw_sender, count in tqdm_or_plain(counts, desc="Checking List-Unsubscribe support"):
        email_match = re.search(r"(?<=<)(.*)(?=>)", raw_sender)
        address = email_match.group() if email_match else raw_sender

        support = gmail.check_unsubscribe_support(address)

        senders.append(
            {
                "sender": raw_sender,
                "address": address,
                "unread_count": count,
                "list_unsubscribe": support,
            }
        )

    return senders


def tqdm_or_plain(iterable, desc: str = ""):
    """Small indirection so this module doesn't hard-depend on tqdm's
    exact import path — matches the pattern the rest of the project uses.
    """
    from tqdm import tqdm

    return tqdm(iterable, desc=desc, unit="sender")


def build_report(
    gmail,
    provider_name: str = "anthropic",
    model: Optional[str] = None,
    limit: int = 60,
    days: int = 7,
) -> List[Dict[str, Any]]:
    """Gather data, classify it, and return the merged rows — the raw
    material both format_report_text() and any future formatter use.
    """
    senders = gather_sender_data(gmail, limit=limit, days=days)
    existing_labels = [label["name"] for label in gmail.list_labels("me")]

    provider = get_provider(provider_name, model=model)
    classifications = provider.classify_senders(senders, existing_labels)

    by_address = {s["address"]: s for s in senders}
    rows = []
    for c in classifications:
        addr = c.get("sender", "")
        base = by_address.get(addr, {})
        classification = c.get("classification", "keep")
        if classification not in CLASSIFICATIONS:
            classification = "keep"  # fail safe, not fail loud-and-wrong

        rows.append(
            {
                "sender": addr or base.get("sender", "unknown"),
                "unread_count": base.get("unread_count", "?"),
                "classification": classification,
                "reason": c.get("reason", ""),
                "one_click_available": base.get("list_unsubscribe", {}).get("kind") == "one_click",
            }
        )

    return rows


def format_report_text(rows: List[Dict[str, Any]]) -> str:
    delete_unsub = [r for r in rows if r["classification"] == "delete_and_unsubscribe"]
    delete_only = [r for r in rows if r["classification"] == "delete_only"]
    keep = [r for r in rows if r["classification"] == "keep"]

    lines = ["Weekly Gmail Clutter Report", "=" * 30, ""]

    lines.append(f"Suggested: delete + unsubscribe ({len(delete_unsub)} senders)")
    if delete_unsub:
        for r in delete_unsub:
            tag = " [one-click available]" if r["one_click_available"] else ""
            lines.append(f"  - {r['sender']} ({r['unread_count']} unread){tag} — {r['reason']}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append(f"Suggested: delete only, not enough evidence to auto-unsubscribe ({len(delete_only)} senders)")
    if delete_only:
        for r in delete_only:
            lines.append(f"  - {r['sender']} ({r['unread_count']} unread) — {r['reason']}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append(f"Left alone ({len(keep)} senders) — financial, personal, labeled, or otherwise not clearly clutter")
    for r in keep:
        lines.append(f"  - {r['sender']} ({r['unread_count']} unread) — {r['reason']}")

    lines.append("")
    lines.append("-" * 30)
    lines.append("This is a suggestion only. Nothing above has been deleted or unsubscribed.")
    lines.append("To act on any of it, use the trash/unsubscribe menu options with these sender addresses.")

    return "\n".join(lines)
