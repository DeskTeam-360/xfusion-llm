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
from database import get_chunks_by_category, get_vector_store
from llm_common import estimate_cost_usd, extract_token_counts, parse_json_object, resolve_insight_model
from security import verify_api_key

logger = logging.getLogger("xfusion-backend.routers.qbr")

router = APIRouter(
    prefix="/api/v1/qbr",
    tags=["Quarterly Business Review"],
    dependencies=[Depends(verify_api_key)],
)

QBR_KNOWLEDGE_CATEGORY = "fusion_qbr"
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

COR_CAPABILITIES = ("alignment", "accountability", "communication", "leadership", "execution")
TREND_VALUES = {"up", "down", "flat"}
CAPABILITY_LABELS = {"Strength", "Developing", "Opportunity", "No data"}


class AssessmentRequest(BaseModel):
    qbr_id: int = Field(..., ge=1)
    evidence: Dict[str, Any] = Field(default_factory=dict)
    model: Optional[str] = Field(None, description="Optional insight model override")
    system_prompt: Optional[str] = Field(None, description="Full system prompt override")


class SynthesisRequest(BaseModel):
    qbr_id: int = Field(..., ge=1)
    context: Dict[str, Any] = Field(default_factory=dict)
    model: Optional[str] = Field(None, description="Optional insight model override")
    system_prompt: Optional[str] = Field(None, description="Full system prompt override")


class AiGenerationResponse(BaseModel):
    model: str
    tokens_used: int
    cost_usd: float
    prompt_tokens: int = 0
    completion_tokens: int = 0


class AssessmentResponse(AiGenerationResponse):
    assessment: Dict[str, Any]


class SynthesisResponse(AiGenerationResponse):
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


def _load_qbr_knowledge() -> str:
    try:
        db = get_vector_store()
        chunks = get_chunks_by_category(db, QBR_KNOWLEDGE_CATEGORY)
        if chunks:
            return "\n\n---\n\n".join(chunks[:12])
    except Exception as err:
        logger.warning("Could not load QBR knowledge from ChromaDB: %s", err)
    return ""


def _score_100(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        num = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, num))


def _trend(value: Any) -> Optional[str]:
    key = str(value or "").strip().lower()
    if key in TREND_VALUES:
        return key
    return None


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


def _capability_label(score: Optional[int]) -> str:
    if score is None:
        return "No data"
    if score >= 80:
        return "Strength"
    if score >= 50:
        return "Developing"
    return "Opportunity"


def _normalize_capability_assessment(raw: Any, evidence: Dict[str, Any]) -> List[Dict[str, Any]]:
    by_capability: Dict[str, Dict[str, Any]] = {}

    if isinstance(raw, list):
        for row in raw:
            if not isinstance(row, dict):
                continue
            cap = str(row.get("capability") or "").strip().lower()
            if cap not in COR_CAPABILITIES:
                continue
            score = _score_100(row.get("score"))
            label = str(row.get("label") or "").strip()
            if label not in CAPABILITY_LABELS:
                label = _capability_label(score)
            by_capability[cap] = {
                "capability": cap,
                "score": score,
                "label": label,
            }

    trends = evidence.get("cor_capability_trends")
    if isinstance(trends, list):
        for row in trends:
            if not isinstance(row, dict):
                continue
            cap = str(row.get("capability") or "").strip().lower()
            if cap not in COR_CAPABILITIES or cap in by_capability:
                continue
            raw_score = row.get("score")
            score = None
            if raw_score is not None:
                try:
                    score = _score_100(float(raw_score) * 20)
                except (TypeError, ValueError):
                    score = None
            by_capability[cap] = {
                "capability": cap,
                "score": score,
                "label": _capability_label(score),
            }

    out: List[Dict[str, Any]] = []
    for cap in COR_CAPABILITIES:
        out.append(by_capability.get(cap, {"capability": cap, "score": None, "label": "No data"}))
    return out


def _normalize_assessment(data: Dict[str, Any], evidence: Dict[str, Any]) -> Dict[str, Any]:
    overall = data.get("overall_readiness") if isinstance(data.get("overall_readiness"), dict) else {}
    confidence = data.get("confidence_level") if isinstance(data.get("confidence_level"), dict) else {}

    evidence_score = _score_100(evidence.get("overall_readiness_score"))
    score = _score_100(overall.get("score"))
    if score is None:
        score = evidence_score

    trend = _trend(overall.get("trend"))
    if trend is None:
        trend = _trend(evidence.get("overall_readiness_trend"))

    label = str(overall.get("label") or "").strip()
    if not label:
        if score is None:
            label = "No data"
        elif score >= 70:
            label = "Strong"
        elif score >= 50:
            label = "Moderate Strength"
        else:
            label = "Needs Attention"

    confidence_percent = _score_100(confidence.get("percent"))
    if confidence_percent is None:
        confidence_percent = 60 if score is not None else 20

    confidence_label = str(confidence.get("label") or "").strip()
    if not confidence_label:
        confidence_label = "Based on available quarterly evidence coverage."

    strengths = _string_list(data.get("top_strengths"), 5)
    opportunities = _string_list(data.get("top_opportunities"), 5)
    risks = _string_list(data.get("emerging_risks"), 5)
    opportunity_signals = _string_list(data.get("emerging_opportunities"), 5)

    if not strengths:
        strengths = ["Review Individual Insights™ coverage to identify organizational strengths this quarter."]
    if not opportunities:
        opportunities = ["No clear opportunity area surfaced — revisit once more evaluations are completed."]
    if not risks:
        risks = ["No emerging risks identified from the evidence gathered this quarter."]
    if not opportunity_signals:
        opportunity_signals = ["Use this review to re-confirm ARP objective ownership and timelines."]

    return {
        "overall_readiness": {
            "score": score,
            "label": label,
            "trend": trend,
        },
        "confidence_level": {
            "percent": confidence_percent,
            "label": confidence_label,
        },
        "cor_capability_assessment": _normalize_capability_assessment(
            data.get("cor_capability_assessment"), evidence
        ),
        "top_strengths": strengths,
        "top_opportunities": opportunities,
        "emerging_risks": risks,
        "emerging_opportunities": opportunity_signals,
    }


def _commitment_summary_from_rows(commitments: List[Dict[str, Any]]) -> Dict[str, int]:
    total = 0
    high_priority = 0
    in_progress = 0
    not_started = 0

    for raw in commitments:
        if not isinstance(raw, dict):
            continue
        total += 1
        priority = str(raw.get("priority") or "").strip().lower()
        status = str(raw.get("status") or "open").strip().lower()
        if priority == "high":
            high_priority += 1
        if status == "in_progress":
            in_progress += 1
        elif status in {"open", "not_started", ""}:
            not_started += 1

    return {
        "total": total,
        "high_priority": high_priority,
        "in_progress": in_progress,
        "not_started": not_started,
    }


def _normalize_synthesis(data: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    evidence = context.get("evidence") if isinstance(context.get("evidence"), dict) else {}
    assessment = context.get("assessment") if isinstance(context.get("assessment"), dict) else {}
    commitments_raw = context.get("commitments")
    commitments = commitments_raw if isinstance(commitments_raw, list) else []

    leadership_context = context.get("leadership_context")
    discussion_notes = context.get("discussion_notes")

    readiness_block = data.get("organizational_readiness_summary")
    if not isinstance(readiness_block, dict):
        readiness_block = {}

    overall = assessment.get("overall_readiness") if isinstance(assessment.get("overall_readiness"), dict) else {}
    score = _score_100(readiness_block.get("score"))
    if score is None:
        score = _score_100(evidence.get("overall_readiness_score"))
    if score is None:
        score = _score_100(overall.get("score"))

    trend = _trend(readiness_block.get("trend"))
    if trend is None:
        trend = _trend(evidence.get("overall_readiness_trend"))
    if trend is None:
        trend = _trend(overall.get("trend"))

    narrative = str(readiness_block.get("narrative") or "").strip()
    if not narrative:
        if score is not None:
            narrative = (
                "Readiness is improving based on available evidence."
                if trend == "up"
                else "Readiness is declining based on available evidence."
                if trend == "down"
                else "Readiness is holding steady based on available evidence."
            )
        else:
            narrative = "Not enough evaluation coverage this quarter to establish a readiness trend."

    executive_summary = str(data.get("executive_summary") or "").strip()
    if not executive_summary:
        if score is not None:
            trend_text = f" ({trend} vs last quarter)" if trend else ""
            executive_summary = (
                f"This quarter's organizational readiness score is {score}/100{trend_text}. "
                f"{len(commitments)} commitment(s) have been established for the upcoming quarter."
            )
        else:
            executive_summary = "Insufficient evidence was available to compute a numeric readiness score this quarter."

    strengths = _string_list(data.get("organizational_strengths"), 5)
    if not strengths:
        strengths = _string_list(assessment.get("top_strengths"), 5)

    opportunities = _string_list(data.get("organizational_opportunities"), 5)
    if not opportunities:
        opportunities = _string_list(assessment.get("top_opportunities"), 5)

    risks = _string_list(data.get("key_risks"), 5)
    if not risks:
        risks = _string_list(assessment.get("emerging_risks"), 5)

    quarterly_focus = _string_list(data.get("quarterly_focus"), 5)
    if not quarterly_focus:
        quarterly_focus = [
            str(c.get("title") or "").strip()
            for c in commitments
            if isinstance(c, dict) and str(c.get("title") or "").strip()
        ][:5]
    if not quarterly_focus:
        quarterly_focus = ["No commitments recorded yet — add them in Step 5 before publishing."]

    llm_summary = data.get("commitment_summary")
    if not isinstance(llm_summary, dict):
        llm_summary = {}
    computed_summary = _commitment_summary_from_rows(commitments)
    commitment_summary = {
        "total": computed_summary["total"] if computed_summary["total"] else max(0, int(llm_summary.get("total") or 0)),
        "high_priority": computed_summary["high_priority"]
        if computed_summary["total"]
        else max(0, int(llm_summary.get("high_priority") or 0)),
        "in_progress": computed_summary["in_progress"]
        if computed_summary["total"]
        else max(0, int(llm_summary.get("in_progress") or 0)),
        "not_started": computed_summary["not_started"]
        if computed_summary["total"]
        else max(0, int(llm_summary.get("not_started") or 0)),
    }

    attention = _string_list(data.get("recommended_areas_of_attention"), 4)
    if not attention:
        attention = [s for s in [opportunities[0] if opportunities else None, risks[0] if risks else None] if s][:3]

    leadership_considered = bool(
        isinstance(data.get("leadership_context_considered"), bool)
        and data.get("leadership_context_considered")
    ) or (leadership_context is not None and str(leadership_context).strip() != "")

    discussion_considered = bool(
        isinstance(data.get("discussion_notes_considered"), bool)
        and data.get("discussion_notes_considered")
    ) or (discussion_notes is not None and str(discussion_notes).strip() != "")

    return {
        "executive_summary": executive_summary,
        "organizational_readiness_summary": {
            "score": score,
            "trend": trend,
            "narrative": narrative,
        },
        "organizational_strengths": strengths,
        "organizational_opportunities": opportunities,
        "key_risks": risks,
        "quarterly_focus": quarterly_focus,
        "commitment_summary": commitment_summary,
        "recommended_areas_of_attention": attention,
        "leadership_context_considered": leadership_considered,
        "discussion_notes_considered": discussion_considered,
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
        logger.error("Failed to parse QBR JSON: %s | raw=%s", err, content[:500])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI returned invalid JSON. Please retry.",
        ) from err

    return parsed, model, prompt_tokens, completion_tokens, total_tokens


@router.post("/assessment", response_model=AssessmentResponse)
async def organizational_assessment(payload: AssessmentRequest) -> AssessmentResponse:
    """
    Generate QBR Step 3 AI Organizational Assessment from quarterly evidence snapshot.
    Called by Laravel QbrAiService.
    """
    model = resolve_insight_model(payload.model)
    knowledge = _load_qbr_knowledge()
    custom_system = (payload.system_prompt or "").strip()
    system_prompt = custom_system if custom_system else _load_prompt(
        "qbr_assessment_system.md",
        "Return JSON for QBR organizational assessment with overall_readiness, confidence_level, "
        "cor_capability_assessment, top_strengths, top_opportunities, emerging_risks, emerging_opportunities.",
    )

    user_prompt = (
        f"FUSION QBR Framework knowledge (category {QBR_KNOWLEDGE_CATEGORY}):\n"
        f"{knowledge or '(no indexed knowledge yet)'}\n\n"
        f"QBR ID: {payload.qbr_id}\n\n"
        "Analyze this quarterly evidence snapshot and return the assessment JSON:\n"
        f"{json.dumps(payload.evidence, ensure_ascii=False, indent=2)}"
    )

    parsed, used_model, prompt_tokens, completion_tokens, total_tokens = _run_json_completion(
        system_prompt, user_prompt, model
    )
    assessment = _normalize_assessment(parsed, payload.evidence)
    cost = estimate_cost_usd(used_model, prompt_tokens, completion_tokens)

    return AssessmentResponse(
        assessment=assessment,
        model=used_model,
        tokens_used=total_tokens,
        cost_usd=cost,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


@router.post("/synthesis", response_model=SynthesisResponse)
async def organizational_synthesis(payload: SynthesisRequest) -> SynthesisResponse:
    """
    Generate QBR Step 6 AI Organizational Synthesis from evidence, assessment, and commitments.
    Called by Laravel QbrAiService.
    """
    model = resolve_insight_model(payload.model)
    knowledge = _load_qbr_knowledge()
    custom_system = (payload.system_prompt or "").strip()
    system_prompt = custom_system if custom_system else _load_prompt(
        "qbr_synthesis_system.md",
        "Return JSON for QBR organizational synthesis with executive_summary, "
        "organizational_readiness_summary, organizational_strengths, organizational_opportunities, "
        "key_risks, quarterly_focus, commitment_summary, recommended_areas_of_attention, "
        "leadership_context_considered, discussion_notes_considered.",
    )

    user_prompt = (
        f"FUSION QBR Framework knowledge (category {QBR_KNOWLEDGE_CATEGORY}):\n"
        f"{knowledge or '(no indexed knowledge yet)'}\n\n"
        f"QBR ID: {payload.qbr_id}\n\n"
        "Synthesize this quarterly review context and return the synthesis JSON:\n"
        f"{json.dumps(payload.context, ensure_ascii=False, indent=2)}"
    )

    parsed, used_model, prompt_tokens, completion_tokens, total_tokens = _run_json_completion(
        system_prompt, user_prompt, model
    )
    synthesis = _normalize_synthesis(parsed, payload.context)
    cost = estimate_cost_usd(used_model, prompt_tokens, completion_tokens)

    return SynthesisResponse(
        synthesis=synthesis,
        model=used_model,
        tokens_used=total_tokens,
        cost_usd=cost,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
