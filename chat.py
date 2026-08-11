"""
Handles the "Any Questions?" chat that follows an on-demand summary.

Guardrails are architectural, not just prompted:
- Claude is never given any tool here - no function-calling, no code
  execution, no database access during the call. It receives the
  session's already-fetched grounding data (context_data, stored by
  /summary, never re-queried here) plus plain text, and returns plain
  text. There is no code path in this module or api.py's /chat where
  the user's message reaches SQL, a shell, or any write operation.
- The user's message is used exactly two ways: logged as a plain
  string via chat_store.log_message() (a parameterized query, same as
  every other write in this project), and passed to Claude as
  conversational text for it to read and respond to. Nothing else
  touches it - it is never interpolated into a query, never executed,
  never treated as anything but text.
- Staying on-topic and keeping answers short are prompt-level, since
  those are about response quality, not about what the system will
  let happen - CHAT_SYSTEM_PROMPT below is where that's enforced.

Two token-reduction changes: the system prompt (which embeds the full
context_data) is marked cache_control=ephemeral, so a session's
repeated questions - the same context resent on every turn - read it
from cache after the first call instead of reprocessing it fresh each
time; the content Claude sees is byte-identical either way, only the
cost of repeating it changes. Each product's hourly breakdown (never
daily - see _compact_context_data's docstring for why) is rewritten
as a compact table via compact.to_compact_table() instead of a list
of objects - verified by round-tripping real extracted data back to
the original list of dicts and confirming exact equality.
"""
import anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_TIMEOUT_SECONDS, LOG_CLAUDE_PROMPTS
from compact import to_compact_table
from logging_setup import get_agent_logger

MODEL = "claude-sonnet-5"

logger = get_agent_logger(__name__)

CHAT_SYSTEM_PROMPT = """You are answering follow-up questions about a performance summary DTPay already generated and showed to a dashboard user. The exact data behind that summary - every product's numbers, reasons, operators, and daily/hourly breakdowns - is given to you below as JSON. That JSON is the only source of truth you have. Each product's `hourly` (when present) is given as a compact table rather than a list of objects: the first line names the columns (hour, total_resolved, completed, conversion_rate_pct), and each line after that is one hour's values in that same order, comma-separated - the same numbers a list of objects would give you, just not repeating the column names on every line.

Answer only from that data. If a question asks about something it doesn't cover (a different date range, a different product, anything unrelated to this specific summary), say so plainly rather than guessing, estimating, or answering from general knowledge about DTPay or payments.

Keep answers to 1-2 sentences by default. Only go longer if the question genuinely needs it - comparing several products, for instance - and even then stay as tight as the question allows.

Respond in exactly one continuous paragraph, always - no matter how many separate points you're covering, never insert a line break or blank line anywhere in your answer. This is a plain-text popup with no markdown rendering and no visible line breaks, so a multi-paragraph answer doesn't display as tidy paragraphs - it shows up as broken text with stray characters where the breaks were. This rule doesn't bend for "but there are three distinct recommendations to cover" - weave all of them into that same single paragraph using prose connectors ("first,... a second thing worth trying is...; finally,...") instead of separating them visually in any way. No markdown either: no asterisks for bold or italic, no numbered or bulleted lists, no headers - the plain "First,... Second,..." wording is fine, a rendered list is not.

Warm and helpful in tone, never curt or dismissive - including when the honest answer is that something's already covered elsewhere, like a question asking for recommendations that the summary already gave. Point them to it helpfully ("the main things worth trying are still X and Y from the summary - happy to go deeper on either") rather than opening with a correction like "the summary already includes...".

Unlike a written summary, you may state specific numbers directly here when asked (a conversion rate, a count) - the number only appears in your answer, not duplicated anywhere else in this exchange, so there's no restatement/mismatch risk the way there is in the original summary text. Only ever use figures that are actually present in the data below; never estimate, round in a way that invents a number, or compute something not directly derivable from what's given.

You have no ability to look anything up, run any query, or take any action - you can only read the data below and the conversation so far, and reply with text. If asked to do anything else (change data, fetch something not in the JSON below, act on the user's behalf), say plainly that you can only answer questions about this summary's data.

Data for this summary:
{context_json}
"""

FALLBACK_ANSWER = "Sorry, I couldn't process that question just now - please try again."


def _compact_context_data(context_data):
    """
    Returns a new dict, same shape as context_data, with each
    product's hourly breakdown rewritten as a compact table (see
    compact.py) - never mutates the input, since context_data here is
    the exact dict stored in agent_chat_sessions and read fresh on
    every turn in the session; each call builds its own compacted
    copy rather than touching the stored original.

    daily is deliberately left untouched, unlike hourly: each day's
    entry in context_data carries its own reasons/operators breakdown
    (the fix for answering "what were the errors on day X"), which a
    flat numeric table has no way to represent. hourly never had any
    such per-row detail to lose - it's only ever
    total_resolved/completed/conversion_rate_pct - and can run to
    hundreds of rows for a longer range, so it's the one actually
    worth compacting here; daily tops out at MAX_DATE_RANGE_DAYS rows,
    where the savings wouldn't be worth the added risk anyway.
    """
    compacted = dict(context_data)
    compacted["products"] = []
    for product in context_data.get("products", []):
        p = dict(product)
        if p.get("hourly"):
            p["hourly"] = to_compact_table(
                p["hourly"],
                columns=["hour", "total_resolved", "completed", "conversion_rate_pct"],
            )
        compacted["products"].append(p)
    return compacted


def ask(context_data, recent_messages, question, client=None):
    """
    context_data: the grounding data /summary generated for this
    session (the same products/overall payload from its response),
    already JSON-serializable.
    recent_messages: the last few {"role", "message"} dicts for this
    session from chat_store.get_recent_messages(), oldest first.
    Returns a dict: answer, input_tokens, output_tokens,
    cache_creation_input_tokens, cache_read_input_tokens. The last two
    matter because of prompt caching (see module docstring) - a
    cached system prompt shows up as a small input_tokens and a large
    cache_read_input_tokens instead, so input_tokens alone drastically
    understates what the call actually cost; log all of these, not
    just input_tokens, or token tracking silently goes blind to most
    of the cost the moment caching kicks in. Any failure returns
    FALLBACK_ANSWER with every token field None rather than raising -
    a chat answer failing shouldn't surface as a server error to
    someone typing in a popup. Logged to agent.log either way.
    """
    import json

    failure_result = {
        "answer": FALLBACK_ANSWER, "input_tokens": None, "output_tokens": None,
        "cache_creation_input_tokens": None, "cache_read_input_tokens": None,
    }

    client = client or anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=CLAUDE_TIMEOUT_SECONDS)
    system_prompt = CHAT_SYSTEM_PROMPT.format(context_json=json.dumps(_compact_context_data(context_data)))

    messages = [{"role": m["role"], "content": m["message"]} for m in recent_messages]
    messages.append({"role": "user", "content": question})

    if LOG_CLAUDE_PROMPTS:
        logger.info(
            "Chat request:\n--- system (includes full context_data) ---\n%s\n--- messages ---\n%s",
            system_prompt, json.dumps(messages, indent=2),
        )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            thinking={"type": "disabled"},
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
        )
    except Exception as exc:
        logger.error("Chat call failed: %s", exc)
        return dict(failure_result)

    text_blocks = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    if not text_blocks:
        block_types = [getattr(b, "type", type(b).__name__) for b in response.content]
        logger.error("Chat response had no text block - block types were: %s", block_types)
        return dict(failure_result)

    answer = "".join(text_blocks)
    if LOG_CLAUDE_PROMPTS:
        logger.info("Chat response:\n%s", answer)
    return {
        "answer": answer,
        "input_tokens": getattr(response.usage, "input_tokens", None),
        "output_tokens": getattr(response.usage, "output_tokens", None),
        "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", None),
        "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", None),
    }