"""
app/services/model_registry.py
---------------------------------
Admin-side management of the AI model registry and what users see of it
(CLAUDE.md 7.9). An admin can add any model from any supported provider
(app/providers/llm) without code; as soon as it is enabled, users see it in
GET /models. Disabling a model hides it at once; users who had picked it
fall back to the default chain automatically (the router skips it).
"""
from __future__ import annotations

from urllib.parse import urlparse

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.ai import AiModel, TaskModelDefault, UserModelPref
from app.models.user import User
from app.providers.llm import PROVIDERS
from app.services import ai_router, key_store


def _bad(message: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, message)


def validate_tasks(tasks: list[str]) -> list[str]:
    unknown = [t for t in tasks if t not in ai_router.TASKS]
    if unknown:
        raise _bad(f"Unknown task(s): {', '.join(unknown)}. Allowed: {', '.join(ai_router.TASKS)}.")
    return sorted(set(tasks))


def validate_base_url(url: str | None) -> str | None:
    if url is None:
        return None
    url = url.strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise _bad("base_url must be a full http(s) URL, e.g. https://openrouter.ai/api/v1")
    if parsed.username or parsed.password:
        raise _bad("base_url must not contain credentials; store the key under Keys instead.")
    return url


def default_key_scope(provider: str, base_url: str | None) -> str:
    """Native providers use their own name. OpenAI-compatible endpoints use
    the main part of the host: api.openai.com -> openai, openrouter.ai ->
    openrouter, localhost -> localhost."""
    if provider != "openai_compatible" or not base_url:
        return provider
    host = (urlparse(base_url).hostname or "custom").lower()
    parts = [p for p in host.split(".") if p not in ("api", "www")]
    if len(parts) >= 2:
        return key_store.normalize_scope(parts[-2])
    return key_store.normalize_scope(parts[0] if parts else "custom")


def get_model(db: Session, model_ref: str) -> AiModel:
    model = db.get(AiModel, model_ref)
    if model is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no model found with this id")
    return model


def snapshot(m: AiModel) -> dict:
    return {
        "provider": m.provider, "model_id": m.model_id, "display_name": m.display_name, "tier": m.tier,
        "tasks": list(m.tasks or []), "base_url": m.base_url, "key_scope": m.key_scope,
        "cost_per_1k_in": m.cost_per_1k_in, "cost_per_1k_out": m.cost_per_1k_out,
        "enabled_global": m.enabled_global, "priority": m.priority,
    }


def admin_view(db: Session, m: AiModel) -> dict:
    return {
        "id": m.id, "provider": m.provider, "model_id": m.model_id, "display_name": m.display_name,
        "tier": m.tier, "tasks": list(m.tasks or []), "base_url": m.base_url, "key_scope": m.key_scope,
        "key_configured": key_store.platform_key_available(db, m.key_scope),
        "cost_per_1k_in": m.cost_per_1k_in, "cost_per_1k_out": m.cost_per_1k_out,
        "context_window": m.context_window, "max_output_tokens": m.max_output_tokens,
        "enabled_global": m.enabled_global, "priority": m.priority, "notes": m.notes,
        "health": {"consecutive_failures": m.consecutive_failures or 0, "last_success_at": m.last_success_at,
                   "last_error_at": m.last_error_at, "last_error": m.last_error,
                   "paused": ai_router.breaker_open(m.id)},
        "created_at": m.created_at, "updated_at": m.updated_at,
    }


def create_model(db: Session, data: dict, *, actor_id: str) -> AiModel:
    if data["provider"] not in PROVIDERS:
        raise _bad(f"provider must be one of {', '.join(PROVIDERS)}")
    base_url = validate_base_url(data.get("base_url"))
    if data["provider"] == "openai_compatible" and not base_url:
        raise _bad("OpenAI-compatible models need a base_url.")
    if data["provider"] != "openai_compatible" and base_url:
        raise _bad("base_url is only used by openai_compatible models.")
    model_id = data["model_id"].strip()
    dup = db.query(AiModel).filter(AiModel.provider == data["provider"], AiModel.model_id == model_id)
    dup = dup.filter(AiModel.base_url == base_url) if base_url else dup.filter(AiModel.base_url.is_(None))
    if dup.first():
        raise HTTPException(status.HTTP_409_CONFLICT, "This model is already in the registry.")

    key_scope = data.get("key_scope")
    model = AiModel(
        provider=data["provider"], model_id=model_id, display_name=data["display_name"].strip(),
        tier=data.get("tier", "free"), tasks=validate_tasks(data.get("tasks") or []), base_url=base_url,
        key_scope=key_store.normalize_scope(key_scope) if key_scope else default_key_scope(data["provider"], base_url),
        cost_per_1k_in=data.get("cost_per_1k_in") or 0.0, cost_per_1k_out=data.get("cost_per_1k_out") or 0.0,
        context_window=data.get("context_window"), max_output_tokens=data.get("max_output_tokens"),
        enabled_global=data.get("enabled_global", True), priority=data.get("priority", 100),
        notes=data.get("notes"), created_by=actor_id,
    )
    db.add(model)
    db.flush()
    return model


def update_model(db: Session, model: AiModel, changes: dict) -> AiModel:
    if "tasks" in changes and changes["tasks"] is not None:
        changes["tasks"] = validate_tasks(changes["tasks"])
    if "base_url" in changes:
        if model.provider != "openai_compatible":
            raise _bad("base_url is only used by openai_compatible models.")
        changes["base_url"] = validate_base_url(changes["base_url"])
        if not changes["base_url"]:
            raise _bad("OpenAI-compatible models need a base_url.")
    if changes.get("key_scope"):
        changes["key_scope"] = key_store.normalize_scope(changes["key_scope"])
    for field, value in changes.items():
        if value is None and field not in ("context_window", "max_output_tokens", "notes"):
            continue
        setattr(model, field, value)
    db.flush()
    return model


def delete_model(db: Session, model: AiModel) -> None:
    """Removes the model and every choice/default pointing at it. Ledger
    rows keep the model's name, so past usage stays readable."""
    db.query(UserModelPref).filter(UserModelPref.model_ref == model.id).delete()
    db.query(TaskModelDefault).filter(TaskModelDefault.model_ref == model.id).delete()
    db.delete(model)
    db.flush()


def defaults_map(db: Session) -> dict[str, str]:
    return {d.task: d.model_ref for d in db.query(TaskModelDefault).all()}


def set_defaults(db: Session, defaults: dict[str, str | None], *, actor_id: str) -> dict[str, str]:
    for task, model_ref in defaults.items():
        if task not in ai_router.TASKS:
            raise _bad(f"Unknown task {task!r}.")
        row = db.get(TaskModelDefault, task)
        if model_ref is None:
            if row:
                db.delete(row)
            continue
        model = get_model(db, model_ref)
        if not ai_router.serves(model, task):
            raise _bad(f"{model.display_name} is not set up for the {task!r} task.")
        if row is None:
            db.add(TaskModelDefault(task=task, model_ref=model.id, updated_by=actor_id))
        else:
            row.model_ref = model.id
            row.updated_by = actor_id
    db.flush()
    return defaults_map(db)


# ---------------------------------------------------------------- user side

def user_catalog(db: Session, user: User) -> dict:
    """Every enabled model, with whether this user can use it and why not."""
    scopes = key_store.user_key_scopes(db, user.id)
    models = (db.query(AiModel).filter(AiModel.enabled_global.is_(True))
              .order_by(AiModel.tier, AiModel.priority, AiModel.display_name).all())
    prefs = {p.task: p.model_ref for p in db.query(UserModelPref).filter(UserModelPref.user_id == user.id).all()}
    out = []
    for m in models:
        verdict = ai_router.access(db, user, m, user_scopes=scopes)
        out.append({
            "id": m.id, "display_name": m.display_name, "provider": m.provider, "model_id": m.model_id,
            "tier": m.tier, "tasks": list(m.tasks or []), "key_scope": m.key_scope,
            "available": verdict.allowed, "reason": verdict.reason, "uses_own_key": m.key_scope in scopes,
            "paused": ai_router.breaker_open(m.id),
        })
    enabled_ids = {m.id for m in models}
    return {
        "tasks": list(ai_router.TASKS),
        "models": out,
        "preferences": {t: ref for t, ref in prefs.items() if ref in enabled_ids},
        "defaults": {t: ref for t, ref in defaults_map(db).items() if ref in enabled_ids},
        "free_only": bool(user.free_only_mode),
        "premium_models": ai_router.may_use_paid(db, user),
    }


def set_user_preferences(db: Session, user: User, preferences: dict[str, str | None]) -> None:
    scopes = key_store.user_key_scopes(db, user.id)
    for task, model_ref in preferences.items():
        if task not in ai_router.TASKS:
            raise _bad(f"Unknown task {task!r}.")
        row = db.query(UserModelPref).filter(UserModelPref.user_id == user.id, UserModelPref.task == task).first()
        if model_ref is None:
            if row:
                db.delete(row)
            continue
        model = db.get(AiModel, model_ref)
        if model is None or not model.enabled_global:
            raise _bad("That model isn't available. Pick one from the list.")
        if not ai_router.serves(model, task):
            raise _bad(f"{model.display_name} can't be used for {task!r}.")
        verdict = ai_router.access(db, user, model, user_scopes=scopes)
        if not verdict.allowed:
            raise _bad(f"You can't use {model.display_name}: {verdict.reason}")
        if row is None:
            db.add(UserModelPref(user_id=user.id, task=task, model_ref=model.id))
        else:
            row.model_ref = model.id
    db.flush()


def key_scopes_in_use(db: Session) -> list[str]:
    """Scopes a user might want a key for: enabled models + key-using sources."""
    from app.models.ai import ProviderCatalog
    scopes = {m.key_scope for m in db.query(AiModel).filter(AiModel.enabled_global.is_(True)).all()}
    scopes |= {r.key_scope for r in db.query(ProviderCatalog).filter(ProviderCatalog.key_scope.isnot(None)).all()}
    return sorted(s for s in scopes if s)
