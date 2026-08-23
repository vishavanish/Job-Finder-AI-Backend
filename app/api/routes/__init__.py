from fastapi import APIRouter

from app.api.routes import applications, apply, company_ats, pipeline, score, search, tasks, auth

api_router = APIRouter()
api_router.include_router(search.router)
api_router.include_router(company_ats.router)
api_router.include_router(score.router)
api_router.include_router(apply.router)
api_router.include_router(pipeline.router)
api_router.include_router(tasks.router)
api_router.include_router(auth.router)

api_router.include_router(applications.router)