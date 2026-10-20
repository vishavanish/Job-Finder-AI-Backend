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
from app.models.admin import AuditLog, FeatureFlag, UserEntitlement  # noqa: F401
from app.models.ai import (  # noqa: F401
    AiModel, ProviderCatalog, Secret, TaskModelDefault, UsageLedger, UserModelPref, UserProviderSetting,
)
from app.models.profile import JobPreference, Profile, Resume, ResumeFile, SearchProfile  # noqa: F401
from app.models.jobs import Job, JobSourceSeen, UserJob  # noqa: F401
from app.models.alerts import JobAlert, JobAlertRun  # noqa: F401
from app.models.apply import AnswerBankEntry, ApplyPack, ApplyProof, ApplyRun, ApplySettings  # noqa: F401
from app.models.email import EmailAccount, EmailEvent, EmailJob  # noqa: F401
from app.models.ats import AtsCompany, AtsJob, AtsPlatformSetting  # noqa: F401
from app.models.voice import StoryBankEntry, VoiceFeedback, VoiceSession, VoiceSettings, VoiceTurn  # noqa: F401
