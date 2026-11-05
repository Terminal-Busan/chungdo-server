"""Frontend/backend contract for Welfare Next."""

from datetime import datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

T = TypeVar("T")
Status = Literal["pending", "running", "done", "failed"]


class Success(BaseModel, Generic[T]):
    data: T
    message: str = "ok"


class Error(BaseModel):
    statusCode: int
    message: str


class Health(BaseModel):
    status: Literal["ok"] = "ok"


class PolicySummary(BaseModel):
    id: str
    name: str
    field: str = ""
    agency: str = ""
    region: str | None = None


class PolicyDetail(BaseModel):
    id: str
    name: str
    summary: str = ""
    target_text: str = ""
    criteria_text: str = ""
    conditions: dict[str, str | int | float | None] = Field(default_factory=dict)
    documents: str = ""
    apply_method: str = ""
    apply_deadline: str = ""
    online_apply_url: str = ""
    detail_url: str = ""
    gov24_updated_at: str = ""


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)


class Profile(RequestModel):
    age: int = Field(ge=0, le=120)
    sido: str = Field(min_length=1, max_length=100)
    sigungu: str | None = Field(default=None, max_length=100)
    household_type: Literal["single", "couple", "single_parent", "multi_child", "other"]
    household_size: int = Field(ge=1, le=10)
    income_monthly_manwon: int = Field(ge=0)
    housing_type: Literal["owned", "jeonse", "monthly_rent", "other"]
    employment_status: Literal["employed", "job_seeker", "student", "none"]
    has_disability: StrictBool = False


class NavigationCreate(RequestModel):
    applied_policy_id: str = Field(min_length=1, max_length=200)
    rejection_text: str = Field(default="", max_length=2000)
    profile: Profile
    consent: StrictBool

    @field_validator("consent")
    @classmethod
    def require_consent(cls, value: bool) -> bool:
        if not value:
            raise ValueError("개인정보 임시 저장 동의가 필요합니다")
        return value


class NavigationCreated(BaseModel):
    id: str
    status: Literal["pending"] = "pending"


class Criterion(BaseModel):
    name: str
    status: Literal["pass", "fail", "unknown"]
    detail: str


class FailedCriterion(BaseModel):
    criterion: str
    user_value: str
    required_value: str
    explanation: str


class GlossaryEntry(BaseModel):
    term: str
    meaning: str


class Rejection(BaseModel):
    failed_criteria: list[FailedCriterion]
    plain_summary: str
    glossary: list[GlossaryEntry]
    ai_generated: bool


class Alternative(BaseModel):
    policy_id: str
    name: str
    match: Literal["eligible", "likely", "unlikely"]
    criteria: list[Criterion]
    reason: str
    ai_generated: bool


class NextAction(BaseModel):
    type: Literal["reapply", "documents", "appeal", "apply_new"]
    title: str
    description: str
    documents: list[str] = Field(default_factory=list)
    policy_id: str | None = None


class PathwayEntry(BaseModel):
    order: int
    policy_id: str
    why: str


class AnalysisResult(BaseModel):
    rejection: Rejection
    alternatives: list[Alternative]
    next_actions: list[NextAction]
    pathway: list[PathwayEntry]
    disclaimer: str = "최종 자격 판단은 담당 기관에 있습니다."


class NavigationView(BaseModel):
    id: str
    status: Status
    result: AnalysisResult | None
    expires_at: datetime


class QuestionCreate(RequestModel):
    question: str = Field(min_length=1, max_length=500)


class QuestionAnswer(BaseModel):
    answer: str
    ai_generated: bool


class Deleted(BaseModel):
    deleted: Literal[True] = True
