"""
Replaces every occurrence of a known wallet-aggregator name with
"DTPay" in whatever's about to be returned to a caller - partners
shouldn't need to know which processor DTPay uses behind the scenes.

Deliberately applied at exactly one place: right before a response
leaves api.py, on a copy of the data, never on the original. Several
things upstream genuinely need the real provider name to work at all:
- providers.py's PROVIDER_RULES is keyed by the real name.
- chat.py's system prompt tells Claude that "operator" means a mobile
  network for one provider and a payment channel for another -
  sanitizing the provider name before that reaches Claude would
  destroy the one signal that distinguishes them.
- What gets cached in Redis and what's stored in a chat session's
  context_data both need to stay real too, since a cache hit or a
  later chat turn reads that same data again - sanitizing it once
  would mean every subsequent read is already-lossy.
- agent_chat_logs is an internal audit trail, not something a partner
  sees - no reason to sanitize what gets logged there either.

Names list includes all four wallet_providers ever established in
this project (pawapay, ampere, razorpay, thirdpay), plus the literal
word "thirdparty" as stated, in case that wasn't a stand-in for
thirdpay - costs nothing to include both.
"""
import re

_AGGREGATOR_NAMES = ["pawapay", "ampere", "razorpay", "thirdpay", "thirdparty"]
_AGGREGATOR_PATTERN = re.compile("|".join(re.escape(name) for name in _AGGREGATOR_NAMES), re.IGNORECASE)


def sanitize_aggregator_names(data):
    """
    Recursively walks a JSON-like structure (dicts, lists, strings;
    anything else - numbers, dates, None - passes through unchanged)
    and replaces every case-insensitive occurrence of a known
    aggregator name with "DTPay". Returns a new structure; never
    mutates the input, since the same data this is called on is also
    what gets cached/stored/logged elsewhere and must stay real there.

    One real edge case worth knowing about, not silently hidden: a
    couple of pawapay's own error messages embed a URL
    (status.pawapay.cloud) - this replaces "pawapay" inside that URL
    too, since the alternative (carving out an exception for text that
    happens to look like a URL) would mean the aggregator name still
    leaks through in exactly the kind of raw, vendor-sourced text this
    is meant to catch. The result is a URL that no longer resolves,
    which is a fair trade against still naming the vendor.
    """
    if isinstance(data, dict):
        return {k: sanitize_aggregator_names(v) for k, v in data.items()}
    if isinstance(data, list):
        return [sanitize_aggregator_names(v) for v in data]
    if isinstance(data, str):
        return _AGGREGATOR_PATTERN.sub("DTPay", data)
    return data
