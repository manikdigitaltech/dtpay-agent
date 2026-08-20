"""
On-demand performance summary API, plus the follow-up chat.

POST /summary - JWT in the Authorization header, (uid, start_date,
end_date, optionally cp_product_id) in the body - returns a JSON
summary covering whichever products the requesting uid is allowed to
see. Now checks summary_cache first (see that module for the key
design and why session_id is never part of what's cached) - on a hit,
skips straight to creating a fresh chat session from the cached data;
on a miss, runs the full extract -> rollup -> analyze pipeline as
before, then caches the result for next time.

POST /chat - JWT in the header, (uid, session_id, message) in the
body - answers a question about that session's data. See chat.py for
the guardrail design (no tools, text in and text out only); this file
just adds the ownership check (a session belongs to exactly the uid
that created it, unless the requester is ADMIN) and the history/log
plumbing around it.

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
from providers import PROVIDER_RULES
from rollup import rollup_by_partner
from analyze import analyze_partner
from chat import ask as ask_chat
from chat_store import create_session, get_session, log_message, get_recent_messages, count_user_messages
import summary_cache
from response_sanitizer import sanitize_aggregator_names

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["POST"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


ON_DEMAND_MIN_RESOLVED = 1
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


def _comparison_entries(entries, label):
    return [{label: e["operator"], "attempts": e["attempts"], "ok": e["operator_ok"]} for e in entries]


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

    # Cache check happens AFTER auth/ownership resolve above, never
    # before - a cache hit must never bypass the authorization check
    # that decided cp_product_ids in the first place.
    cache_key = summary_cache.build_key(request.start_date, request.end_date, request.cp_product_id, request.uid)
    cached = summary_cache.get(cache_key)
    if cached is not None:
        session_id = str(uuid4())
        create_session(
            session_id=session_id, uid=request.uid, start_date=request.start_date,
            end_date=request.end_date, cp_product_id=request.cp_product_id,
            context_data={"date_range": cached["date_range"], "overall": cached["overall"], "products": cached["products"]},
        )
        return sanitize_aggregator_names({"session_id": session_id, **cached})

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
            comparison_label = PROVIDER_RULES.get(service["provider"], {}).get("comparison_label", "operator")
            product = {
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
                "reasons": [
                    {"reason": r["reason_code"], "occurrences": r["occurrences"]}
                    for r in service.get("reasons", [])
                ],
                "daily": [
                    {"date": d["date"].isoformat(), "total_resolved": d["total_resolved"],
                     "completed": d["completed"], "conversion_rate_pct": d["conversion_rate_pct"],
                     "reasons": [{"reason": r["reason_code"], "occurrences": r["occurrences"]} for r in d.get("reasons", [])],
                     comparison_label + "s": _comparison_entries(d.get("operators", []), comparison_label)}
                    for d in service.get("daily", [])
                ],
                "hourly": [
                    {"hour": h["hour"].isoformat(), "total_resolved": h["total_resolved"],
                     "completed": h["completed"], "conversion_rate_pct": h["conversion_rate_pct"]}
                    for h in service.get("hourly", [])
                ] if include_hourly else None,
            }
            product[comparison_label + "s"] = _comparison_entries(service.get("operators", []), comparison_label)
            products.append(product)
    products.sort(key=lambda p: (p["cp_id"], p["product_id"]))

    overall_rate = round(total_completed / total_resolved * 100, 2) if total_resolved else 0.0

    result_data = {
        "date_range": {"start": request.start_date.isoformat(), "end": request.end_date.isoformat()},
        "overall": {
            "total_resolved": total_resolved,
            "completed": total_completed,
            "conversion_rate_pct": overall_rate,
        },
        "products": products,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    ttl = summary_cache.determine_ttl(request.start_date, request.end_date, date.today())
    summary_cache.set(cache_key, result_data, ttl)

    context_data = {"date_range": result_data["date_range"], "overall": result_data["overall"], "products": products}
    create_session(
        session_id=session_id, uid=request.uid, start_date=request.start_date,
        end_date=request.end_date, cp_product_id=request.cp_product_id, context_data=context_data,
    )

    return sanitize_aggregator_names({"session_id": session_id, **result_data})


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

    return {"session_id": request.session_id, "answer": sanitize_aggregator_names(result["answer"])}