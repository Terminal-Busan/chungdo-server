"""Public API DTOs. Field names follow the frontend API contract."""

from datetime import datetime
from typing import Annotated, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
)

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Identifier = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
]
T = TypeVar("T")


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Envelope(BaseModel, Generic[T]):
    data: T
    message: str = "ok"


class Profile(DTO):
    age: Annotated[StrictInt, Field(ge=0, le=120)]
    sido: Annotated[Text, Field(max_length=50)]
    sigungu: Annotated[Text, Field(max_length=50)] | None = None
    household_type: Literal["single", "couple", "single_parent", "multi_child", "other"]
    household_size: Annotated[StrictInt, Field(ge=1, le=10)]
    income_monthly_manwon: Annotated[StrictInt, Field(ge=0)]
    housing_type: Literal["owned", "jeonse", "monthly_rent", "other"]
    employment_status: Literal["employed", "job_seeker", "student", "none"]
    has_disability: StrictBool = False


class CreateNavigation(DTO):
    applied_policy_id: Identifier
    rejection_text: Annotated[str, Field(max_length=2000)] | None = None
    profile: Profile
    consent: StrictBool


class Question(DTO):
    question: Annotated[Text, Field(max_length=500)]


class Criterion(DTO):
    name: str
    status: Literal["pass", "fail", "unknown"]
    detail: str


class FailedCriterion(DTO):
    criterion: str
    user_value: str
    required_value: str
    explanation: str


class Glossary(DTO):
    term: str
    meaning: str


class Rejection(DTO):
    failed_criteria: list[FailedCriterion]
    plain_summary: str
    glossary: list[Glossary]
    ai_generated: bool


class Alternative(DTO):
    policy_id: str
    name: str
    match: Literal["eligible", "likely", "unlikely"]
    criteria: list[Criterion]
    reason: str
    ai_generated: bool


class NextAction(DTO):
    type: Literal["reapply", "documents", "appeal", "apply_new"]
    title: str
    description: str
    documents: list[str]
    policy_id: str


class Pathway(DTO):
    order: int
    policy_id: str
    why: str


class AnalysisResult(DTO):
    rejection: Rejection
    alternatives: list[Alternative]
    next_actions: list[NextAction]
    pathway: list[Pathway]
    disclaimer: str = "최종 자격 판단은 담당 기관에 있습니다."


class SummaryOutput(DTO):
    plain_summary: Annotated[Text, Field(max_length=600)]


class PossibleReason(DTO):
    reason: Annotated[Text, Field(max_length=300)]
    basis: Literal["통지 문구", "선정기준 원문"]


class ExplanationOutput(SummaryOutput):
    failed_criteria: list[FailedCriterion] = Field(max_length=20)
    possible_reasons: list[PossibleReason] = Field(max_length=3)
    glossary: list[Glossary] = Field(min_length=2, max_length=5)


class MatchedPolicy(DTO):
    policy_id: Identifier
    reason: Annotated[Text, Field(max_length=300)]


class MatchOutput(DTO):
    alternatives: list[MatchedPolicy] = Field(max_length=5)


class AgentAction(DTO):
    type: Literal["reapply", "documents", "appeal", "apply_new"]
    title: Annotated[Text, Field(max_length=200)]
    description: Annotated[Text, Field(max_length=1000)]
    documents: list[str] = Field(max_length=20)
    policy_id: Identifier | None


class GuidanceOutput(DTO):
    next_actions: list[AgentAction] = Field(min_length=3, max_length=6)
    pathway: list[Pathway] = Field(max_length=5)


class PolicySummary(DTO):
    id: str
    name: str
    field: str | None
    agency: str | None
    region: str | None


class PolicyDetail(DTO):
    id: str
    name: str
    summary: str | None
    target_text: str | None
    criteria_text: str | None
    conditions: dict
    documents: str | None
    apply_method: str | None
    apply_deadline: str | None
    online_apply_url: str | None
    detail_url: str | None
    gov24_updated_at: str | None


class SessionCreated(DTO):
    id: UUID
    status: Literal["pending"]


class SessionResult(DTO):
    id: UUID
    status: Literal["pending", "running", "done", "failed"]
    result: AnalysisResult | None
    expires_at: datetime


class Answer(DTO):
    answer: str
    ai_generated: bool


class Deleted(DTO):
    deleted: bool


class ErrorResponse(DTO):
    statusCode: int
    message: str
