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

logger = logging.getLogger("xfusion-backend.routers.arp")

router = APIRouter(
    prefix="/api/v1/arp",
    tags=["Annual Readiness Plan"],
    dependencies=[Depends(verify_api_key)],
)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


class ReadinessReviewRequest(BaseModel):
    arp_id: int = Field(..., ge=1)
    plan_context: Dict[str, Any] = Field(default_factory=dict)
    model: Optional[str] = Field(None, description="Optional insight model override")
    system_prompt: Optional[str] = Field(None, description="Full system prompt override from WordPress")


class ReadinessReviewResponse(BaseModel):
    assessment: Dict[str, Any]
    model: str
    tokens_used: int
    cost_usd: float
    prompt_tokens: int = 0
    completion_tokens: int = 0


def _load_prompt(filename: str, fallback: str) -> str:
    path = PROMPTS_DIR / filename
    try:
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError as err:
        logger.warning("Could not read prompt %s: %s", path, err)
    return fallback


def _score(value: Any, default: int = 70) -> int:
    try:
        num = int(round(float(value)))
    except (TypeError, ValueError):
        num = default
    return max(0, min(100, num))


def _level(value: Any) -> str:
    key = str(value or "").strip().lower()
    if key == "high":
        return "High"
    if key == "low":
        return "Low"
    return "Medium"


def _donut_color(score: int, override: Any = None) -> str:
    color = str(override or "").strip()
    if color.startswith("#"):
        return color
    if score >= 80:
        return "#5f9a3f"
    if score >= 65:
        return "#c4a035"
    return "#ea580c"


def _string_list(raw: Any, max_items: int = 8) -> List[str]:
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


def _normalize_assessment(data: Dict[str, Any]) -> Dict[str, Any]:
    sa = data.get("strategic_alignment") if isinstance(data.get("strategic_alignment"), dict) else {}
    ra = data.get("readiness_assessment") if isinstance(data.get("readiness_assessment"), dict) else {}
    pa = data.get("priority_alignment") if isinstance(data.get("priority_alignment"), dict) else {}
    rs = data.get("risk_summary") if isinstance(data.get("risk_summary"), dict) else {}

    sa_score = _score(sa.get("score"), 84)
    ra_score = _score(ra.get("score"), 76)
    pa_score = _score(pa.get("score"), 82)

    gaps_out: List[Dict[str, str]] = []
    gaps_raw = data.get("gaps")
    if isinstance(gaps_raw, list):
        for gap in gaps_raw:
            if not isinstance(gap, dict):
                continue
            area = str(gap.get("area") or "").strip()
            if not area:
                continue
            gaps_out.append(
                {
                    "area": area,
                    "description": str(gap.get("description") or "").strip(),
                    "impact": _level(gap.get("impact")),
                    "priority": _level(gap.get("priority")),
                }
            )

    dims_out: List[Dict[str, Any]] = []
    dims_raw = pa.get("dimensions")
    if isinstance(dims_raw, list):
        for dim in dims_raw:
            if not isinstance(dim, dict):
                continue
            label = str(dim.get("label") or "").strip()
            if not label:
                continue
            dims_out.append(
                {
                    "label": label,
                    "percent": _score(dim.get("percent"), 75),
                }
            )

    return {
        "strategic_alignment": {
            "score": sa_score,
            "label": str(sa.get("label") or "Strong Alignment").strip() or "Strong Alignment",
            "color": _donut_color(sa_score, sa.get("color")),
            "summary": str(sa.get("summary") or "").strip(),
            "strengths": _string_list(sa.get("strengths"), 6),
        },
        "readiness_assessment": {
            "score": ra_score,
            "label": str(ra.get("label") or "Readiness Score").strip() or "Readiness Score",
            "color": _donut_color(ra_score, ra.get("color")),
            "summary": str(ra.get("summary") or "").strip(),
            "strengths_count": max(0, int(ra.get("strengths_count") or 0)),
            "development_count": max(0, int(ra.get("development_count") or 0)),
            "critical_gaps_count": max(0, int(ra.get("critical_gaps_count") or 0)),
        },
        "gaps": gaps_out,
        "priority_alignment": {
            "score": pa_score,
            "label": str(pa.get("label") or "Alignment Score").strip() or "Alignment Score",
            "color": _donut_color(pa_score, pa.get("color")),
            "summary": str(pa.get("summary") or "").strip(),
            "dimensions": dims_out,
        },
        "risk_summary": {
            "high": max(0, int(rs.get("high") or 0)),
            "medium": max(0, int(rs.get("medium") or 0)),
            "low": max(0, int(rs.get("low") or 0)),
            "strengths": max(0, int(rs.get("strengths") or 0)),
        },
        "focus_areas": _string_list(data.get("focus_areas"), 8),
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
        logger.error("Failed to parse ARP readiness-review JSON: %s | raw=%s", err, content[:500])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI returned invalid JSON. Please retry.",
        ) from err

    return parsed, model, prompt_tokens, completion_tokens, total_tokens


@router.post("/readiness-review", response_model=ReadinessReviewResponse)
async def readiness_review(payload: ReadinessReviewRequest) -> ReadinessReviewResponse:
    """
    Generate ARP Step 6 AI Readiness Review from Steps 1–5 plan context.
    Called by Laravel ArpAiService.
    """
    model = resolve_insight_model(payload.model)
    custom_system = (payload.system_prompt or "").strip()
    system_prompt = custom_system if custom_system else _load_prompt(
        "arp_readiness_review_system.md",
        "Return JSON for ARP AI Readiness Review with strategic_alignment, readiness_assessment, "
        "gaps, priority_alignment, risk_summary, and focus_areas.",
    )

    user_prompt = (
        f"ARP ID: {payload.arp_id}\n\n"
        "Analyze this Annual Readiness Plan context and return the assessment JSON:\n"
        f"{json.dumps(payload.plan_context, ensure_ascii=False, indent=2)}"
    )

    parsed, used_model, prompt_tokens, completion_tokens, total_tokens = _run_json_completion(
        system_prompt, user_prompt, model
    )
    assessment = _normalize_assessment(parsed)
    cost = estimate_cost_usd(used_model, prompt_tokens, completion_tokens)

    return ReadinessReviewResponse(
        assessment=assessment,
        model=used_model,
        tokens_used=total_tokens,
        cost_usd=cost,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
