"""
Provider-agnostic interface for the weekly clutter report's judgment step.

Volume and List-Unsubscribe presence are mechanical signals the rest of
this tool can compute without any AI. What they can't do is reason about
a specific sender the way a person reviewing their own inbox would — e.g.
recognizing that a sender matches a label the user deliberately created,
or that an address at a real company is a marketing persona rather than a
personal contact. That judgment call is what this module delegates to an
LLM, and it's intentionally swappable: add a provider by implementing
AIProvider and registering it in PROVIDERS, no other code needs to change.

Both providers share one system prompt so behavior stays consistent
regardless of which one you pick — the goal is the same judgment, not
provider-specific quirks.
"""

import json
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

CLASSIFICATION_SYSTEM_PROMPT = """You are triaging unread Gmail senders for a weekly cleanup report.

You will be given a JSON object with:
- "senders": a list of {sender, address, unread_count, list_unsubscribe} \
  entries. list_unsubscribe.supported tells you whether the sender's mail \
  carries a List-Unsubscribe header (a strong signal of bulk/marketing mail \
  — real people and transactional systems don't send it).
- "existing_labels": Gmail labels the user has already created. A sender \
  whose name/domain matches one of these was very likely organized there \
  on purpose, not left as clutter.

Classify every sender in the input as exactly one of:
- "delete_and_unsubscribe": high-confidence bulk marketing/newsletter clutter
- "delete_only": looks like clutter, but not enough evidence to auto-unsubscribe
- "keep": anything that could be personal, financial, medical, legal, \
  school- or government-related, matches an existing label, or is \
  otherwise not clearly bulk mail

Be conservative. When uncertain, classify as "keep" — leaving clutter in \
the inbox costs nothing; suggesting deletion of something that matters is \
the failure mode to avoid. Watch specifically for: personal names at \
gmail.com/yahoo.com/etc. (individual people, not companies), bank/credit \
union/insurance/investment-account senders (not their marketing arms — the \
marketing arm of a bank is fine to flag, the actual statements/alerts are \
not), medical providers, schools, government domains, and any sender \
matching an existing label.

Respond with ONLY a JSON array, one object per sender, no other text:
[{"sender": "<address>", "classification": "<one of the three above>", "reason": "<one short sentence>"}]
"""


class AIProvider(ABC):
    @abstractmethod
    def classify_senders(
        self, senders: List[Dict[str, Any]], existing_labels: List[str]
    ) -> List[Dict[str, Any]]:
        """Return a list of {sender, classification, reason} dicts, one per
        input sender.
        """
        raise NotImplementedError


def _parse_json_array(text: str) -> List[Dict[str, Any]]:
    """Models sometimes wrap JSON in markdown fences despite instructions
    not to, and OpenAI's JSON-object mode requires the root to be an
    object rather than an array — handle both without caring which
    provider produced the text.
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    parsed = json.loads(text)
    if isinstance(parsed, dict):
        for value in parsed.values():
            if isinstance(value, list):
                return value
        raise ValueError(f"Expected a JSON array (or an object containing one), got: {parsed!r}")
    return parsed


class AnthropicProvider(AIProvider):
    DEFAULT_MODEL = "claude-sonnet-4-5-20250929"

    def __init__(self, model: Optional[str] = None):
        import anthropic

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. Export it, or add it to a .env "
                "file in the project root."
            )
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model or self.DEFAULT_MODEL

    def classify_senders(self, senders, existing_labels):
        user_content = json.dumps({"senders": senders, "existing_labels": existing_labels})
        response = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=CLASSIFICATION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        return _parse_json_array(response.content[0].text)


class OpenAIProvider(AIProvider):
    DEFAULT_MODEL = "gpt-4o-mini"

    def __init__(self, model: Optional[str] = None):
        import openai

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY not set. Export it, or add it to a .env "
                "file in the project root."
            )
        self.client = openai.OpenAI(api_key=api_key)
        self.model = model or self.DEFAULT_MODEL

    def classify_senders(self, senders, existing_labels):
        # response_format=json_object requires the word "json" in the
        # messages and a JSON *object* (not a bare array) as the root —
        # _parse_json_array() unwraps whichever shape comes back so the
        # rest of the code doesn't need to know the difference.
        user_content = json.dumps(
            {
                "senders": senders,
                "existing_labels": existing_labels,
                "instructions": "Return your classifications as a JSON object: {\"classifications\": [...]}",
            }
        )
        response = self.client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": CLASSIFICATION_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        )
        return _parse_json_array(response.choices[0].message.content)


PROVIDERS = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
}


def get_provider(name: str, model: Optional[str] = None) -> AIProvider:
    """Factory — the only place calling code needs to touch to add a
    provider. Everything else in this module and in report.py only ever
    talks to the AIProvider interface.
    """
    key = name.lower()
    if key not in PROVIDERS:
        raise ValueError(f"Unknown provider '{name}'. Choose from: {', '.join(PROVIDERS)}")
    return PROVIDERS[key](model=model)
