"""
User-controlled whitelist: email addresses or domains that must never be
trashed, spammed, labeled-for-deletion, unsubscribed from, or suggested
for deletion by the weekly report — regardless of what an AI classifier
or a pasted-in query says.

This is deliberately a deterministic, local check, not another prompt
instruction to an LLM. An LLM can be told "never suggest deleting X" and
still get it wrong sometimes; a Python set lookup can't.

Format (whitelist.txt, one entry per line):
  - a line containing "@" is treated as an exact email address
  - a line without "@" is treated as a domain, matching that domain and
    any subdomain of it (e.g. "citi.com" also protects "info15.citi.com")
  - blank lines and lines starting with # are ignored
"""

from pathlib import Path
from typing import Set, Tuple

DEFAULT_WHITELIST_PATH = Path(__file__).resolve().parent.parent / "whitelist.txt"


def _domain_of(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower().strip() if "@" in address else address.lower().strip()


def load_whitelist(path: Path = DEFAULT_WHITELIST_PATH) -> Tuple[Set[str], Set[str]]:
    """Returns (emails, domains) — both lowercased. Missing file is not an
    error; it just means an empty whitelist (nothing protected yet).
    """
    emails: Set[str] = set()
    domains: Set[str] = set()

    if not path.exists():
        return emails, domains

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.lower()
        if "@" in line:
            emails.add(line)
        else:
            domains.add(line)

    return emails, domains


def save_whitelist(emails: Set[str], domains: Set[str], path: Path = DEFAULT_WHITELIST_PATH) -> None:
    lines = [
        "# Addresses and domains here are never trashed, unsubscribed from,",
        "# or suggested for deletion by the weekly report — no matter what",
        "# an AI classification or a pasted-in query says.",
        "#",
        "# One entry per line. A line with \"@\" is an exact address; a line",
        "# without one is a domain (and protects its subdomains too).",
        "",
    ]
    lines.extend(sorted(emails))
    lines.extend(sorted(domains))
    path.write_text("\n".join(lines) + "\n")


def is_whitelisted(address: str, emails: Set[str], domains: Set[str]) -> bool:
    """Exact address match, or the address's domain matches a whitelisted
    domain or one of its parent domains (so "citi.com" also protects
    "info15.citi.com", not just an exact "citi.com" sender).
    """
    if not address:
        return False

    address = address.lower().strip()
    if address in emails:
        return True

    domain = _domain_of(address)
    for wl_domain in domains:
        if domain == wl_domain or domain.endswith("." + wl_domain):
            return True

    return False


def add_entry(entry: str, path: Path = DEFAULT_WHITELIST_PATH) -> None:
    emails, domains = load_whitelist(path)
    entry = entry.lower().strip()
    if "@" in entry:
        emails.add(entry)
    else:
        domains.add(entry)
    save_whitelist(emails, domains, path)


def remove_entry(entry: str, path: Path = DEFAULT_WHITELIST_PATH) -> bool:
    """Returns True if something was actually removed."""
    emails, domains = load_whitelist(path)
    entry = entry.lower().strip()
    if entry in emails:
        emails.remove(entry)
        save_whitelist(emails, domains, path)
        return True
    if entry in domains:
        domains.remove(entry)
        save_whitelist(emails, domains, path)
        return True
    return False
