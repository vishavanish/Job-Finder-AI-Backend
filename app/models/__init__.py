"""
Importing every model module here (and importing THIS package early —
see app/main.py) guarantees every table is registered on Base.metadata
before create_all()/Alembic runs, regardless of which route happens to
import which model first. See app/models/application.py's comment for
the original bug this pattern fixes (NoReferencedTableError).
"""
from app.models.user import User  # noqa: F401
from app.models.application import Application, ApplicationStatusEvent  # noqa: F401
from app.models.pipeline_run import PipelineRun, PipelineRunJob  # noqa: F401