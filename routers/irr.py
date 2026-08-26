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

logger = logging.getLogger("xfusion-backend.routers.irr")

router = APIRouter(
    prefix="/api/v1/360",
    tags=["Individual Readiness Review"],
    dependencies=[Depends(verify_api_key)],
)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

TAG_VALUES = {"High", "Medium", "Low"}


class DevelopmentAssessmentRequest(BaseModel):
    review_id: int = Field(..., ge=1)
    evidence: Dict[str, Any] = Field(default_factory=dict)
    readiness_indicators: Dict[str, Any] = Field(default_factory=dict)
    model: Optional[str] = Field(None, description="Optional insight model override")
    system_prompt: Optional[str] = Field(None, description="Full system prompt override from WordPress")
    prompt_version_id: Optional[str] = Field(None, description="Optional prompt version identifier to echo back")
    prompt_version_label: Optional[str] = Field(None, description="Optional prompt version label to echo back")


class DevelopmentSynthesisRequest(BaseModel):
    review_id: int = Field(..., ge=1)
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


class DevelopmentAssessmentResponse(AiGenerationResponse):
    assessment: Dict[str, Any]


class DevelopmentSynthesisResponse(AiGenerationResponse):
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


def _tag_value(value: Any) -> str:
    text = str(value or "").strip().title()
    return text if text in TAG_VALUES else "Medium"


def _tagged_items(raw: Any, default_tag_label: str, max_items: int = 5) -> List[Dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        out.append(
            {
                "title": title,
                "description": str(item.get("description") or "").strip(),
                "tag_label": str(item.get("tag_label") or default_tag_label).strip() or default_tag_label,
                "tag_value": _tag_value(item.get("tag_value")),
            }
        )
        if len(out) >= max_items:
            break
    return out


def _normalize_development_assessment(data: Dict[str, Any]) -> Dict[str, Any]:
    pattern = data.get("behavioral_pattern_summary") if isinstance(data.get("behavioral_pattern_summary"), dict) else {}

    return {
        "behavioral_strengths": _tagged_items(data.get("behavioral_strengths"), "Evidence", 5),
        "development_opportunities": _tagged_items(data.get("development_opportunities"), "Impact", 5),
        "behavioral_pattern_summary": {
            "summary": str(pattern.get("summary") or "").strip(),
            "primary_pattern": str(pattern.get("primary_pattern") or "").strip(),
            "secondary_pattern": str(pattern.get("secondary_pattern") or "").strip(),
            "energy_pattern": str(pattern.get("energy_pattern") or "").strip(),
            "growth_edge": str(pattern.get("growth_edge") or "").strip(),
        },
        "leadership_contributions": _string_list(data.get("leadership_contributions"), 5),
        "organizational_contribution": _string_list(data.get("organizational_contribution"), 4),
        "key_takeaway": str(data.get("key_takeaway") or "").strip(),
    }


def _opportunity_summary(raw: Any, max_items: int = 3) -> List[Dict[str, str]]:
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


def _development_roadmap(raw: Any, max_items: int = 4) -> List[Dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        period = str(item.get("period") or "").strip()
        text = str(item.get("text") or "").strip()
        if not period and not text:
            continue
        out.append({"period": period, "text": text})
        if len(out) >= max_items:
            break
    return out


def _normalize_development_synthesis(data: Dict[str, Any]) -> Dict[str, Any]:
    strength_summary = data.get("strength_summary") if isinstance(data.get("strength_summary"), dict) else {}
    coaching = data.get("executive_coaching_summary") if isinstance(data.get("executive_coaching_summary"), dict) else {}

    return {
        "annual_development_summary": str(data.get("annual_development_summary") or "").strip(),
        "behavioral_growth_summary": str(data.get("behavioral_growth_summary") or "").strip(),
        "strength_summary": {
            "title": str(strength_summary.get("title") or "").strip(),
            "items": _string_list(strength_summary.get("items"), 4),
        },
        "opportunity_summary": _opportunity_summary(data.get("opportunity_summary"), 3),
        "development_roadmap": _development_roadmap(data.get("development_roadmap"), 4),
        "recommended_focus_areas": _string_list(data.get("recommended_focus_areas"), 5),
        "executive_coaching_summary": {
            "summary": str(coaching.get("summary") or "").strip(),
            "engagement_level": _tag_value(coaching.get("engagement_level")),
            "recommendation": str(coaching.get("recommendation") or "").strip(),
        },
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
        logger.error("Failed to parse IRR JSON: %s | raw=%s", err, content[:500])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI returned invalid JSON. Please retry.",
        ) from err

    return parsed, model, prompt_tokens, completion_tokens, total_tokens


@router.post("/development-assessment", response_model=DevelopmentAssessmentResponse)
async def development_assessment(payload: DevelopmentAssessmentRequest) -> DevelopmentAssessmentResponse:
    """
    Generate IRR Step 3 AI Development Assessment from the per-employee annual
    evidence snapshot. Called by Laravel IrrAiService.
    """
    model = resolve_insight_model(payload.model)
    custom_system = (payload.system_prompt or "").strip()
    system_prompt = custom_system if custom_system else _load_prompt(
        "irr_development_assessment_system.md",
        "Return JSON for the IRR AI Development Assessment with behavioral_strengths, "
        "development_opportunities, behavioral_pattern_summary, leadership_contributions, "
        "organizational_contribution, and key_takeaway.",
    )

    user_prompt = (
        f"Review ID: {payload.review_id}\n\n"
        "Pre-computed readiness_indicators (0-5 scale; context only — never restate or alter these numbers):\n"
        f"{json.dumps(payload.readiness_indicators, ensure_ascii=False, indent=2)}\n\n"
        "Analyze this employee's annual evidence snapshot and return the assessment JSON:\n"
        f"{json.dumps(payload.evidence, ensure_ascii=False, indent=2)}"
    )

    parsed, used_model, prompt_tokens, completion_tokens, total_tokens = _run_json_completion(
        system_prompt, user_prompt, model
    )
    assessment = _normalize_development_assessment(parsed)
    cost = estimate_cost_usd(used_model, prompt_tokens, completion_tokens)

    return DevelopmentAssessmentResponse(
        assessment=assessment,
        model=used_model,
        tokens_used=total_tokens,
        cost_usd=cost,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prompt_version_id=payload.prompt_version_id,
        prompt_version_label=payload.prompt_version_label,
    )


@router.post("/development-synthesis", response_model=DevelopmentSynthesisResponse)
async def development_synthesis(payload: DevelopmentSynthesisRequest) -> DevelopmentSynthesisResponse:
    """
    Generate IRR Step 6 AI Development Synthesis from the annual evidence, Step 3
    assessment, Step 4 conversation notes, and Step 5 commitments. Called by
    Laravel IrrAiService.
    """
    model = resolve_insight_model(payload.model)
    custom_system = (payload.system_prompt or "").strip()
    system_prompt = custom_system if custom_system else _load_prompt(
        "irr_development_synthesis_system.md",
        "Return JSON for the IRR AI Development Synthesis with annual_development_summary, "
        "behavioral_growth_summary, strength_summary, opportunity_summary, development_roadmap, "
        "recommended_focus_areas, and executive_coaching_summary.",
    )

    user_prompt = (
        f"Review ID: {payload.review_id}\n\n"
        "Synthesize this annual development context (evidence, Step 3 assessment, conversation notes, "
        "commitments, and readiness indicators) and return the synthesis JSON:\n"
        f"{json.dumps(payload.context, ensure_ascii=False, indent=2)}"
    )

    parsed, used_model, prompt_tokens, completion_tokens, total_tokens = _run_json_completion(
        system_prompt, user_prompt, model
    )
    synthesis = _normalize_development_synthesis(parsed)
    cost = estimate_cost_usd(used_model, prompt_tokens, completion_tokens)

    return DevelopmentSynthesisResponse(
        synthesis=synthesis,
        model=used_model,
        tokens_used=total_tokens,
        cost_usd=cost,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prompt_version_id=payload.prompt_version_id,
        prompt_version_label=payload.prompt_version_label,
    )
