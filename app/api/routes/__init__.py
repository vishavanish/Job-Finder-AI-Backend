from fastapi import APIRouter

from app.api.routes import (
    admin, admin_ai, alerts, answer_bank, applications, apply, ats, auth, gmail, company_ats, jobs, models, pipeline, profile, providers, score, search,
    tasks,
)

api_router = APIRouter()
api_router.include_router(search.router)
api_router.include_router(company_ats.router)
api_router.include_router(score.router)
api_router.include_router(apply.router)
api_router.include_router(pipeline.router)
api_router.include_router(tasks.router)
api_router.include_router(auth.router)

api_router.include_router(applications.router)
api_router.include_router(admin.router)
api_router.include_router(admin_ai.router)
api_router.include_router(models.router)
api_router.include_router(providers.router)
api_router.include_router(profile.router)
api_router.include_router(jobs.router)
api_router.include_router(alerts.router)
api_router.include_router(answer_bank.router)
api_router.include_router(gmail.router)
api_router.include_router(ats.router)
