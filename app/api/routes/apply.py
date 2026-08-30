from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from app.core.auth import get_current_user
from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.core.task_dispatch import pending_response
from app.models.schemas import ApplyRequest, TaskResponse
from app.core.celery_tasks import apply_task

router = APIRouter(prefix="/apply", tags=["apply"])
logger = logging.getLogger("job_finder_api.apply")
settings = get_settings()


@router.post(
    "",
    response_model=TaskResponse,
    status_code=202,
    dependencies=[Depends(get_current_user)],
)
@limiter.limit(settings.RATE_LIMIT_APPLY)
async def start_apply(request: Request, req: ApplyRequest, user=Depends(get_current_user)):
    """Opens top-matched jobs in a real Playwright browser on the Celery
    worker and pre-fills LinkedIn Easy Apply forms — never auto-submits.
    Poll GET /tasks/{task_id} for status, and GET /applications for the
    tracked application rows (scoped to your account).

    NOTE: browser_profile_dir is intentionally NOT forwarded from the
    request as-is — see _safe_profile_dir below.
    """
    jobs = [j.model_dump() for j in req.jobs]
    apply_params = {
        "applicant_info": req.applicant_info.model_dump(),
        "open_top_n": req.open_top_n,
        "auto_fill_easy_apply": req.auto_fill_easy_apply,
        "pause_between_tabs_sec": req.pause_between_tabs_sec,
        "browser_profile_dir": _safe_profile_dir(req.browser_profile_dir),
        "headless": req.headless,
        "user_id": user.id,
        "require_auto_apply_capable": req.require_auto_apply_capable,
    }

    async_result = apply_task.apply_async(args=[jobs, apply_params], queue=settings.apply_queue)
    # async_result = apply_task.apply_async(args=[jobs, apply_params], queue="apply")
    logger.info("[task=%s] dispatched to 'default' queue for user=%s", async_result.id, user.id)
    return pending_response(async_result)


def _safe_profile_dir(requested: str | None) -> str | None:
    """Refuses any client-supplied browser_profile_dir outright — a
    client-controlled filesystem path here would let any authenticated
    caller make the Celery WORKER process write/read arbitrary paths on
    its own disk."""
    if requested:
        logger.warning("ignored client-supplied browser_profile_dir override")
    return None