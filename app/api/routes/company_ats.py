# app/api/routes/company_ats.py
from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core.auth import get_current_user
from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.core.task_dispatch import pending_response
from app.models.schemas import CompanyAtsRequest, TaskResponse
from app.core.celery_tasks import company_ats_task

router = APIRouter(prefix="/company-ats", tags=["company-ats"])
logger = logging.getLogger("job_finder_api.company_ats")
settings = get_settings()

_PRIVATE_NETWORKS = [
    ipaddress.ip_network(net) for net in (
        "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
        "169.254.0.0/16", "::1/128", "fc00::/7", "fe80::/10",
    )
]


def _reject_private_targets(req: CompanyAtsRequest) -> None:
    """SSRF guard: custom ATS targets let a caller supply an arbitrary
    endpoint_url that the worker will fetch server-side (either via a
    plain HTTP request or, for JS-rendered career pages, via a headless
    Playwright browser — see company_ats_sources.fetch_custom_rendered).
    Block anything that resolves to a private/link-local/loopback address
    (e.g. cloud metadata endpoints) before the task is even dispatched,
    regardless of which fetch strategy ends up being used."""
    for target in req.targets:
        if target.platform != "custom" or not target.custom_endpoint_url:
            continue
        host = urlparse(target.custom_endpoint_url).hostname
        if not host:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid custom_endpoint_url for '{target.slug}'")
        try:
            resolved_ips = {info[4][0] for info in socket.getaddrinfo(host, None)}
        except socket.gaierror:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"could not resolve host for '{target.slug}'")
        for ip_str in resolved_ips:
            ip = ipaddress.ip_address(ip_str)
            if any(ip in net for net in _PRIVATE_NETWORKS):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"custom_endpoint_url for '{target.slug}' resolves to a private/internal address — rejected",
                )


@router.post(
    "/fetch",
    response_model=TaskResponse,
    status_code=202,
    dependencies=[Depends(get_current_user)],
)
@limiter.limit(settings.RATE_LIMIT_SEARCH)
async def fetch_company_ats(request: Request, req: CompanyAtsRequest):
    """Fetches jobs from Greenhouse/Lever/SmartRecruiters/Workday, or a
    custom company career page.

    For platform="custom": the worker first tries a plain JSON API probe
    against custom_endpoint_url. If that returns nothing usable (common
    for JS-rendered career sites, e.g. SuccessFactors-based boards), it
    falls back to a headless-Playwright DOM scrape — but only if you also
    supply custom_list_selector (and optionally custom_title_selector /
    custom_link_selector / custom_location_selector). Without selectors,
    an unscrapeable custom page fails loudly with a message telling you
    what to inspect, rather than silently returning zero jobs.

    Poll GET /tasks/{task_id} for status and results."""
    _reject_private_targets(req)
    targets = [t.model_dump() for t in req.targets]

    async_result = company_ats_task.apply_async(
        args=[targets, req.request_delay_sec], queue=settings.default_queue
    )
    logger.info("[task=%s] dispatched to 'default' queue", async_result.id)
    return pending_response(async_result)