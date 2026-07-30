"""
On-demand performance summary API.

POST /summary with a JWT in the Authorization header and (uid,
start_date, end_date, optionally cp_product_id) in the body - returns
a JSON summary covering whichever products the requesting uid is
allowed to see: every approved product (or just cp_product_id, if
given) for an ADMIN; only that uid's own products for anyone else.

Reuses the same extract -> rollup -> analyze pipeline the weekly
email uses - the only genuinely new logic here is auth.py and the
HTTP plumbing. Each partner still gets exactly one Claude call
(rollup_by_partner groups by partner internally even though the
response below is flattened back into one list), so an ADMIN request
spanning many partners makes one call per partner, not one giant call
mixing everyone together.

Run with: uvicorn api:app --host 0.0.0.0 --port 8000
"""
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from auth import authenticate, get_role, resolve_cp_product_ids, AuthError
from extract import fetch_all
from rollup import rollup_by_partner
from analyze import analyze_partner

app = FastAPI()

# Unlike the weekly email, this endpoint has no real volume floor -
# the dashboard it's attached to already shows low-volume rows
# without hiding them (a 1-attempt product is a real row in the
# screenshot this was built from), so summarizing on demand shouldn't
# silently drop what's already visible on screen. analyze.py's system
# prompt is told to flag low volume explicitly instead of hiding it.
ON_DEMAND_MIN_RESOLVED = 1


class SummaryRequest(BaseModel):
    uid: int
    start_date: date
    end_date: date
    cp_product_id: Optional[int] = None


def _extract_token(authorization: str) -> str:
    prefix = "bearer "
    if authorization.lower().startswith(prefix):
        return authorization[len(prefix):].strip()
    return authorization.strip()


@app.post("/summary")
def create_summary(request: SummaryRequest, authorization: str = Header(...)):
    token = _extract_token(authorization)

    try:
        authenticate(request.uid, token)
        role_name = get_role(request.uid)
        cp_product_ids = resolve_cp_product_ids(request.uid, role_name, request.cp_product_id)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    day_start = datetime.combine(request.start_date, datetime.min.time())
    day_end = datetime.combine(request.end_date, datetime.min.time()) + timedelta(days=1)

    product_metrics, reason_breakdown, operator_breakdown, _ = fetch_all(
        day_start, day_end, cp_product_ids, min_resolved=ON_DEMAND_MIN_RESOLVED
    )

    digests = rollup_by_partner(product_metrics, reason_breakdown, operator_breakdown)
    for digest in digests:
        analyze_partner(digest)

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
            })
    products.sort(key=lambda p: (p["cp_id"], p["product_id"]))

    overall_rate = round(total_completed / total_resolved * 100, 2) if total_resolved else 0.0

    return {
        "date_range": {"start": request.start_date.isoformat(), "end": request.end_date.isoformat()},
        "overall": {
            "total_resolved": total_resolved,
            "completed": total_completed,
            "conversion_rate_pct": overall_rate,
        },
        "products": products,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
