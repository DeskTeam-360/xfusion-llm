import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from langchain_community.callbacks.manager import get_openai_callback
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from config import settings
from llm_common import estimate_cost_usd, extract_token_counts, parse_json_object, resolve_insight_model
from security import verify_api_key

logger = logging.getLogger("xfusion-backend.routers.arr")

router = APIRouter(
    prefix="/api/v1/arr",
    tags=["Annual Readiness Review"],
    dependencies=[Depends(verify_api_key)],
)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


class AnnualAssessmentRequest(BaseModel):
    arr_id: int = Field(..., ge=1)
    evidence: Dict[str, Any] = Field(default_factory=dict)
    readiness_indicators: Dict[str, Any] = Field(default_factory=dict)
    model: Optional[str] = Field(None, description="Optional insight model override")
    system_prompt: Optional[str] = Field(None, description="Full system prompt override from WordPress")
    prompt_version_id: Optional[str] = Field(None, description="Optional prompt version identifier to echo back")
    prompt_version_label: Optional[str] = Field(None, description="Optional prompt version label to echo back")


class StrategicRenewalSynthesisRequest(BaseModel):
    arr_id: int = Field(..., ge=1)
    context: Dict[str, Any] = Field(default_factory=dict)
    model: Optional[str] = Field(None, description="Optional insight model override")
    system_prompt: Optional[str] = Field(None, description="Full system prompt override from WordPress")
    prompt_version_id: Optional[str] = Field(None, description="Optional prompt version identifier to echo back")
    prompt_version_label: Optional[str] = Field(None, description="Optional prompt version label to echo back")


class AiGenerationResponse(BaseModel):
    model: str
    tokens_used: int
    cost_usd: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_version_id: Optional[str] = None
    prompt_version_label: Optional[str] = None


class AnnualAssessmentResponse(AiGenerationResponse):
    assessment: Dict[str, Any]


class StrategicRenewalSynthesisResponse(AiGenerationResponse):
    synthesis: Dict[str, Any]


def _load_prompt(filename: str, fallback: str) -> str:
    path = PROMPTS_DIR / filename
    try:
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError as err:
        logger.warning("Could not read prompt %s: %s", path, err)
    return fallback


def _string_list(raw: Any, max_items: int = 5) -> List[str]:
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text:
            out.append(text)
        if len(out) >= max_items:
            break
    return out


def _themes(raw: Any, max_items: int = 5) -> List[Dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        description = str(item.get("description") or "").strip()
        if not title and not description:
            continue
        out.append({"title": title, "description": description})
        if len(out) >= max_items:
            break
    return out


def _normalize_annual_assessment(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "organizational_readiness_narrative": str(data.get("organizational_readiness_narrative") or "").strip(),
        "strategic_alignment_narrative": str(data.get("strategic_alignment_narrative") or "").strip(),
        "leadership_readiness_narrative": str(data.get("leadership_readiness_narrative") or "").strip(),
        "development_trends_narrative": str(data.get("development_trends_narrative") or "").strip(),
        "strategic_risks": _string_list(data.get("strategic_risks"), 5),
        "strategic_opportunities": _string_list(data.get("strategic_opportunities"), 5),
        "emerging_themes": _themes(data.get("emerging_themes"), 5),
        "key_observation": str(data.get("key_observation") or "").strip(),
    }


def _normalize_strategic_renewal_synthesis(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "annual_organizational_learning_summary": str(data.get("annual_organizational_learning_summary") or "").strip(),
        "readiness_progress_summary": str(data.get("readiness_progress_summary") or "").strip(),
        "behavioral_intelligence_summary": str(data.get("behavioral_intelligence_summary") or "").strip(),
        "leadership_intelligence_summary": str(data.get("leadership_intelligence_summary") or "").strip(),
        "strategic_intelligence_summary": str(data.get("strategic_intelligence_summary") or "").strip(),
        "strategic_renewal_summary": str(data.get("strategic_renewal_summary") or "").strip(),
        "recommended_future_focus": _string_list(data.get("recommended_future_focus"), 5),
        "executive_summary": str(data.get("executive_summary") or "").strip(),
    }


def _run_json_completion(system_prompt: str, user_prompt: str, model: str) -> tuple[Dict[str, Any], str, int, int, int]:
    if not settings.OPENAI_API_KEY or settings.OPENAI_API_KEY.startswith("sk-proj-..."):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OPENAI_API_KEY is not configured.",
        )

    llm = ChatOpenAI(model=model, temperature=0.2, openai_api_key=settings.OPENAI_API_KEY)

    with get_openai_callback() as cb:
        response = llm.invoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ])
        prompt_tokens, completion_tokens, total_tokens = extract_token_counts(response, cb)

    content = getattr(response, "content", None)
    if not isinstance(content, str):
        content = str(content or "")

    try:
        parsed = parse_json_object(content)
    except (json.JSONDecodeError, ValueError) as err:
        logger.error("Failed to parse ARR JSON: %s | raw=%s", err, content[:500])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI returned invalid JSON. Please retry.",
        ) from err

    return parsed, model, prompt_tokens, completion_tokens, total_tokens


@router.post("/annual-assessment", response_model=AnnualAssessmentResponse)
async def annual_assessment(payload: AnnualAssessmentRequest) -> AnnualAssessmentResponse:
    """
    Generate ARR Step 3 AI Annual Readiness Assessment from the organization-wide
    annual evidence snapshot. Called by Laravel ArrAiService.
    """
    model = resolve_insight_model(payload.model)
    custom_system = (payload.system_prompt or "").strip()
    system_prompt = custom_system if custom_system else _load_prompt(
        "arr_annual_assessment_system.md",
        "Return JSON for the ARR AI Annual Readiness Assessment with "
        "organizational_readiness_narrative, strategic_alignment_narrative, "
        "leadership_readiness_narrative, development_trends_narrative, strategic_risks, "
        "strategic_opportunities, emerging_themes, and key_observation.",
    )

    user_prompt = (
        f"ARR ID: {payload.arr_id}\n\n"
        "Pre-computed readiness_indicators (0-5 scale; context only — never restate or alter these numbers):\n"
        f"{json.dumps(payload.readiness_indicators, ensure_ascii=False, indent=2)}\n\n"
        "Analyze this organization-wide annual evidence snapshot and return the assessment JSON:\n"
        f"{json.dumps(payload.evidence, ensure_ascii=False, indent=2)}"
    )

    parsed, used_model, prompt_tokens, completion_tokens, total_tokens = _run_json_completion(
        system_prompt, user_prompt, model
    )
    assessment = _normalize_annual_assessment(parsed)
    cost = estimate_cost_usd(used_model, prompt_tokens, completion_tokens)

    return AnnualAssessmentResponse(
        assessment=assessment,
        model=used_model,
        tokens_used=total_tokens,
        cost_usd=cost,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prompt_version_id=payload.prompt_version_id,
        prompt_version_label=payload.prompt_version_label,
    )


@router.post("/strategic-renewal-synthesis", response_model=StrategicRenewalSynthesisResponse)
async def strategic_renewal_synthesis(payload: StrategicRenewalSynthesisRequest) -> StrategicRenewalSynthesisResponse:
    """
    Generate ARR Step 6 AI Strategic Renewal Synthesis from the annual evidence,
    Step 3 assessment, Step 4 executive reflection, and Step 5 recommendations.
    Called by Laravel ArrAiService.
    """
    model = resolve_insight_model(payload.model)
    custom_system = (payload.system_prompt or "").strip()
    system_prompt = custom_system if custom_system else _load_prompt(
        "arr_strategic_renewal_synthesis_system.md",
        "Return JSON for the ARR AI Strategic Renewal Synthesis with "
        "annual_organizational_learning_summary, readiness_progress_summary, "
        "behavioral_intelligence_summary, leadership_intelligence_summary, "
        "strategic_intelligence_summary, strategic_renewal_summary, recommended_future_focus, "
        "and executive_summary.",
    )

    user_prompt = (
        f"ARR ID: {payload.arr_id}\n\n"
        "Synthesize this annual review context (evidence, Step 3 assessment, executive reflection, "
        "and Step 5 recommendations) and return the synthesis JSON:\n"
        f"{json.dumps(payload.context, ensure_ascii=False, indent=2)}"
    )

    parsed, used_model, prompt_tokens, completion_tokens, total_tokens = _run_json_completion(
        system_prompt, user_prompt, model
    )
    synthesis = _normalize_strategic_renewal_synthesis(parsed)
    cost = estimate_cost_usd(used_model, prompt_tokens, completion_tokens)

    return StrategicRenewalSynthesisResponse(
        synthesis=synthesis,
        model=used_model,
        tokens_used=total_tokens,
        cost_usd=cost,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prompt_version_id=payload.prompt_version_id,
        prompt_version_label=payload.prompt_version_label,
    )
