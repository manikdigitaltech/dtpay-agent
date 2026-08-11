"""
On-demand performance summary API, plus the follow-up chat.

POST /summary - JWT in the Authorization header, (uid, start_date,
end_date, optionally cp_product_id) in the body - returns a JSON
summary covering whichever products the requesting uid is allowed to
see: every approved product (or just cp_product_id, if given) for an
ADMIN; only that uid's own products for anyone else. Also creates a
chat session (stores the exact data behind this summary) and returns
its id as session_id, for the "Any Questions?" button to use.

POST /chat - JWT in the header, (uid, session_id, message) in the
body - answers a question about that session's data. See chat.py for
the guardrail design (no tools, text in and text out only); this file
just adds the ownership check (a session belongs to exactly the uid
that created it, unless the requester is ADMIN) and the history/log
plumbing around it.

Reuses the same extract -> rollup -> analyze pipeline the weekly
email uses - the only genuinely new logic in /summary is auth.py and
the HTTP plumbing. Each partner still gets exactly one Claude call
(rollup_by_partner groups by partner internally even though the
response below is flattened back into one list), so an ADMIN request
spanning many partners makes one call per partner, not one giant call
mixing everyone together.

Deliberately does NOT call merge_with_previous() - an arbitrary date
range (a day, four days, whatever the dashboard's picker is set to)
has no well-defined "previous period" to compare against the way a
calendar week does, so this endpoint never claims one. analyze.py's
prompt only mentions period-over-period comparison at all when a
digest's services carry compare_to_previous=True, which only
merge_with_previous() ever sets.

Run with: uvicorn api:app --host 0.0.0.0 --port 8000
"""
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from auth import authenticate, get_role, resolve_cp_product_ids, AuthError
from config import MAX_DATE_RANGE_DAYS, MAX_QUESTIONS_PER_SESSION, ALLOWED_ORIGINS
from extract import fetch_all
from rollup import rollup_by_partner
from analyze import analyze_partner
from chat import ask as ask_chat
from chat_store import create_session, get_session, log_message, get_recent_messages, count_user_messages

app = FastAPI()

# Deny-by-default: with ALLOWED_ORIGINS unset (empty list), no browser
# origin is allowed to call this at all - set it to your dashboard's
# real domain(s) before pointing a browser-based frontend at this.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["POST"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.get("/health")
def health():
    """Unauthenticated on purpose - a load balancer or uptime monitor
    needs to reach this without a JWT. Returns nothing beyond a bare
    OK; it deliberately doesn't touch the database, so it reflects
    whether the API process itself is up, not whether the DB is
    reachable - a DB-down state should show up as real request
    failures, not as this endpoint also going red."""
    return {"status": "ok"}

# Unlike the weekly email, this endpoint has no real volume floor -
# the dashboard it's attached to already shows low-volume rows
# without hiding them (a 1-attempt product is a real row in the
# screenshot this was built from), so summarizing on demand shouldn't
# silently drop what's already visible on screen. analyze.py's system
# prompt is told to flag low volume explicitly instead of hiding it.
ON_DEMAND_MIN_RESOLVED = 1

# Hourly breakdown is only worth computing (and asking Claude to look
# for hour-level patterns in) for a range short enough that it stays
# readable - beyond this, it's hundreds of rows and the daily
# breakdown alone is the more useful view.
ON_DEMAND_MAX_HOURLY_DAYS = 14


class SummaryRequest(BaseModel):
    uid: int
    start_date: date
    end_date: date
    cp_product_id: Optional[int] = None


class ChatRequest(BaseModel):
    uid: int
    session_id: str
    message: str


def _extract_token(authorization: str) -> str:
    prefix = "bearer "
    if authorization.lower().startswith(prefix):
        return authorization[len(prefix):].strip()
    return authorization.strip()


def _format_reasons(reasons):
    return [{"reason": r["reason_code"], "occurrences": r["occurrences"]} for r in reasons]


def _format_operators(operators):
    return [{"operator": o["operator"], "attempts": o["attempts"], "ok": o["operator_ok"]} for o in operators]


@app.post("/summary")
def create_summary(request: SummaryRequest, authorization: str = Header(...)):
    token = _extract_token(authorization)

    try:
        authenticate(request.uid, token)
        role_name = get_role(request.uid)
        cp_product_ids = resolve_cp_product_ids(request.uid, role_name, request.cp_product_id)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    range_days = (request.end_date - request.start_date).days + 1
    if range_days > MAX_DATE_RANGE_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"Date range spans {range_days} days; the maximum allowed is {MAX_DATE_RANGE_DAYS}.",
        )

    day_start = datetime.combine(request.start_date, datetime.min.time())
    day_end = datetime.combine(request.end_date, datetime.min.time()) + timedelta(days=1)
    include_hourly = range_days <= ON_DEMAND_MAX_HOURLY_DAYS

    (product_metrics, reason_breakdown, operator_breakdown, daily_metrics,
     hourly_metrics, daily_reason_breakdown, daily_operator_breakdown) = fetch_all(
        day_start, day_end, cp_product_ids,
        min_resolved=ON_DEMAND_MIN_RESOLVED,
        include_daily=True,
        include_hourly=include_hourly,
        include_daily_reasons=True,
    )

    digests = rollup_by_partner(
        product_metrics, reason_breakdown, operator_breakdown,
        daily_metrics, request.start_date, request.end_date + timedelta(days=1),
        hourly_metrics if include_hourly else None,
        daily_reason_breakdown, daily_operator_breakdown,
    )

    session_id = str(uuid4())

    for digest in digests:
        analyze_partner(digest)
        log_message(
            source="dashboard_summary", role="assistant", session_id=session_id,
            uid=request.uid, cp_id=digest.get("cp_id"),
            input_tokens=digest.get("input_tokens"), output_tokens=digest.get("output_tokens"),
            cache_creation_input_tokens=digest.get("cache_creation_input_tokens"),
            cache_read_input_tokens=digest.get("cache_read_input_tokens"),
        )

    products = []
    total_resolved = 0
    total_completed = 0
    for digest in digests:
        for service in digest["services"]:
            total_resolved += service["total_resolved"]
            total_completed += service["completed"]
            products.append({
                "product_id": service["product_id"],
                "product_name": service["product_name"],
                "country": service["country"],
                "provider": service["provider"],
                "cp_id": digest["cp_id"],
                "partner_name": digest["partner_name"],
                "total_resolved": service["total_resolved"],
                "completed": service["completed"],
                "conversion_rate_pct": service["conversion_rate_pct"],
                "summary": service.get("summary", ""),
                "recommendations": service.get("recommendations", []),
                "notable_days": service.get("notable_days", []),
                "notable_hours": service.get("notable_hours", []),
                "reasons": _format_reasons(service.get("reasons", [])),
                "operators": _format_operators(service.get("operators", [])),
                "daily": [
                    {"date": d["date"].isoformat(), "total_resolved": d["total_resolved"],
                     "completed": d["completed"], "conversion_rate_pct": d["conversion_rate_pct"],
                     "reasons": _format_reasons(d.get("reasons", [])),
                     "operators": _format_operators(d.get("operators", []))}
                    for d in service.get("daily", [])
                ],
                "hourly": [
                    {"hour": h["hour"].isoformat(), "total_resolved": h["total_resolved"],
                     "completed": h["completed"], "conversion_rate_pct": h["conversion_rate_pct"]}
                    for h in service.get("hourly", [])
                ] if include_hourly else None,
            })
    products.sort(key=lambda p: (p["cp_id"], p["product_id"]))

    overall_rate = round(total_completed / total_resolved * 100, 2) if total_resolved else 0.0
    overall = {
        "total_resolved": total_resolved,
        "completed": total_completed,
        "conversion_rate_pct": overall_rate,
    }

    # Same payload that's returned below becomes the chat session's
    # grounding data - one structure, no separate re-serialization for
    # storage vs. response.
    context_data = {
        "date_range": {"start": request.start_date.isoformat(), "end": request.end_date.isoformat()},
        "overall": overall,
        "products": products,
    }
    create_session(
        session_id=session_id,
        uid=request.uid, start_date=request.start_date, end_date=request.end_date,
        cp_product_id=request.cp_product_id, context_data=context_data,
    )

    return {
        "session_id": session_id,
        "date_range": context_data["date_range"],
        "overall": overall,
        "products": products,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/chat")
def chat(request: ChatRequest, authorization: str = Header(...)):
    token = _extract_token(authorization)

    try:
        authenticate(request.uid, token)
        role_name = get_role(request.uid)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    session = get_session(request.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if datetime.now(timezone.utc) > session["expires_at"].replace(tzinfo=timezone.utc):
        raise HTTPException(status_code=410, detail="Session expired")
    # A session's context_data was already scoped to exactly what its
    # creator (session["uid"]) was allowed to see when /summary built
    # it - this only needs to confirm the requester IS that creator
    # (or an admin), not re-derive product ownership from scratch.
    if session["uid"] != request.uid and role_name != "ADMIN":
        raise HTTPException(status_code=403, detail="This session does not belong to this user")

    asked_so_far = count_user_messages(request.session_id)
    if asked_so_far >= MAX_QUESTIONS_PER_SESSION:
        raise HTTPException(
            status_code=429,
            detail=f"This session has reached its limit of {MAX_QUESTIONS_PER_SESSION} questions. Generate a new summary to keep asking questions.",
        )

    recent = get_recent_messages(request.session_id)

    log_message(source="chat", role="user", session_id=request.session_id,
                uid=request.uid, message=request.message)

    result = ask_chat(session["context_data"], recent, request.message)

    log_message(source="chat", role="assistant", session_id=request.session_id,
                uid=request.uid, message=result["answer"],
                input_tokens=result["input_tokens"], output_tokens=result["output_tokens"],
                cache_creation_input_tokens=result["cache_creation_input_tokens"],
                cache_read_input_tokens=result["cache_read_input_tokens"])

    return {"session_id": request.session_id, "answer": result["answer"]}