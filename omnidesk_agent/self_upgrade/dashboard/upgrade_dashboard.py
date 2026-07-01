from __future__ import annotations
from html import escape

def build_dashboard_html(data: dict) -> str:
    proposals = data.get("proposals", [])
    metrics = data.get("metrics", {})
    rows="\n".join("<tr>"+f"<td>{escape(p.get('proposal_id',''))}</td><td>{escape(p.get('title',''))}</td><td>{escape(str(p.get('score','')))}</td><td>{escape(p.get('risk_level',''))}</td><td>{escape(p.get('status',''))}</td><td>{escape(p.get('upgrade_type',''))}</td>"+"</tr>" for p in proposals)
    return f"""<!doctype html><html><head><meta charset='utf-8'><title>OmniDesk Self-Upgrade Dashboard</title><style>body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;margin:32px}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ddd;padding:8px}}th{{background:#f5f5f5}}.card{{border:1px solid #ddd;padding:16px;border-radius:8px;margin-bottom:18px}}</style></head><body><h1>OmniDesk Self-Upgrade Dashboard</h1><div class='card'><h2>Performance Metrics</h2><pre>{escape(str(metrics))}</pre></div><div class='card'><h2>Upgrade Queue</h2><table><thead><tr><th>ID</th><th>Title</th><th>Score</th><th>Risk</th><th>Status</th><th>Type</th></tr></thead><tbody>{rows}</tbody></table></div></body></html>"""

def create_dashboard_router(runtime, admin_auth=None):
    try:
        from fastapi import APIRouter
        from fastapi.responses import HTMLResponse
    except Exception:
        return None
    router=APIRouter()
    @router.get("/self-upgrade/dashboard", response_class=HTMLResponse)
    async def dashboard(request):
        if admin_auth is not None:
            decision = await admin_auth.verify_request(request, required_role='viewer')
            if not decision.ok:
                from fastapi import HTTPException
                raise HTTPException(status_code=403, detail=decision.reason)
        proposals=[p.to_dict() for p in runtime.proposal_store.list()] if hasattr(runtime,"proposal_store") else []
        metrics=runtime.memory.metrics_report(days=7) if hasattr(runtime,"memory") else {}
        return build_dashboard_html({"proposals":proposals,"metrics":metrics})
    @router.get("/self-upgrade/proposals")
    async def proposals(request):
        if admin_auth is not None:
            decision = await admin_auth.verify_request(request, required_role='viewer')
            if not decision.ok:
                from fastapi import HTTPException
                raise HTTPException(status_code=403, detail=decision.reason)
        return {"proposals":[p.to_dict() for p in runtime.proposal_store.list()]} if hasattr(runtime,"proposal_store") else {"proposals":[]}
    return router
