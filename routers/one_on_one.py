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

logger = logging.getLogger("xfusion-backend.routers.one_on_one")

router = APIRouter(
    prefix="/api/v1/one-on-one",
    tags=["1-on-1 Alignment"],
    dependencies=[Depends(verify_api_key)],
)

ONE_ON_ONE_KNOWLEDGE_CATEGORY = "fusion_one_on_one"
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

BRIEF_SECTION_KEYS = (
    "alignment_snapshot",
    "development_snapshot",
    "commitment_review",
    "behavioral_trends",
    "suggested_discussion_areas",
    "emerging_opportunities",
    "potential_barriers",
)

SYNTHESIS_SECTION_KEYS = (
    "meeting_summary",
    "alignment_summary",
    "development_summary",
    "commitment_summary",
    "emerging_risks",
    "emerging_opportunities",
    "suggested_coaching_topics",
    "recommended_follow_up",
)


class MeetingBriefRequest(BaseModel):
    conversation_id: int = Field(..., ge=1)
    leader_user_id: int = Field(..., ge=1)
    employee_user_id: int = Field(..., ge=1)
    prior_syntheses: List[Any] = Field(default_factory=list)
    evidence_context: Dict[str, Any] = Field(default_factory=dict)
    model: Optional[str] = Field(None, description="Optional insight model override")
    system_prompt: Optional[str] = Field(None, description="Full system prompt from WordPress (overrides default file)")
    prompt_version_id: Optional[str] = Field(None, description="WordPress prompt version id")
    prompt_version_label: Optional[str] = Field(None, description="WordPress prompt version label")


class MeetingSynthesisRequest(BaseModel):
    conversation_id: int = Field(..., ge=1)
    leader_user_id: int = Field(..., ge=1)
    employee_user_id: int = Field(..., ge=1)
    preparations: Dict[str, Any] = Field(default_factory=dict)
    notes: List[Dict[str, Any]] = Field(default_factory=list)
    commitments: List[Dict[str, Any]] = Field(default_factory=list)
    model: Optional[str] = Field(None, description="Optional insight model override")
    system_prompt: Optional[str] = Field(None, description="Full system prompt from WordPress (overrides default file)")
    prompt_version_id: Optional[str] = Field(None, description="WordPress prompt version id")
    prompt_version_label: Optional[str] = Field(None, description="WordPress prompt version label")


class AiGenerationResponse(BaseModel):
    model: str
    tokens_used: int
    cost_usd: float
    prompt_tokens: int = 0
    completion_tokens: int = 0


class MeetingBriefResponse(AiGenerationResponse):
    brief: Dict[str, Any]
    prompt_version_id: Optional[str] = None
    prompt_version_label: Optional[str] = None


class MeetingSynthesisResponse(AiGenerationResponse):
    synthesis: Dict[str, Any]
    prompt_version_id: Optional[str] = None
    prompt_version_label: Optional[str] = None


def _load_prompt(filename: str, fallback: str) -> str:
    path = PROMPTS_DIR / filename
    try:
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError as err:
        logger.warning("Could not read prompt %s: %s", path, err)
    return fallback


def _section_block(raw: Any, *, max_items: int = 4) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {"items": [], "details": ""}
    items = raw.get("items")
    if not isinstance(items, list):
        items = []
    cleaned = [str(item).strip() for item in items if str(item).strip()]
    return {
        "items": cleaned[:max_items],
        "details": str(raw.get("details") or "").strip(),
    }


def _format_commitment_status(status: str) -> str:
    mapping = {
        "in_progress": "In Progress",
        "done": "Done",
        "open": "Open",
    }
    return mapping.get(status, "Open")


def _format_commitment_owner(role: str) -> str:
    mapping = {
        "employee": "Employee",
        "leader": "Leader",
        "shared": "Shared",
    }
    return mapping.get(role, "Shared")


def _sanitize_commitment_rows(commitments: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    seen: set[str] = set()

    for raw in commitments:
        if not isinstance(raw, dict):
            continue

        title = str(raw.get("title") or "").strip()
        if not title:
            continue

        role = str(raw.get("owner_role") or "shared").strip().lower()
        if role not in {"employee", "leader", "shared"}:
            role = "shared"

        status = str(raw.get("status") or "open").strip().lower()
        if status not in {"open", "in_progress", "done"}:
            status = "open"

        row_id = raw.get("id")
        try:
            row_id_int = int(row_id) if row_id is not None else 0
        except (TypeError, ValueError):
            row_id_int = 0

        dedupe_key = f"id:{row_id_int}" if row_id_int > 0 else f"{title.lower()}|{role}|{status}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        rows.append({
            "title": title,
            "owner_role": role,
            "status": status,
        })

    return rows


def _commitment_summary_from_rows(
    commitments: List[Dict[str, Any]],
    llm_block: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    rows = _sanitize_commitment_rows(commitments)
    employee_count = sum(1 for row in rows if row["owner_role"] == "employee")
    leader_count = sum(1 for row in rows if row["owner_role"] == "leader")
    open_count = sum(1 for row in rows if row["status"] != "done")

    llm_details = ""
    if isinstance(llm_block, dict):
        llm_details = str(llm_block.get("details") or "").strip()

    if not rows:
        details = llm_details or "No commitments were saved before synthesis generation."
    else:
        lines = ["Commitments on record:", ""]
        for row in rows:
            lines.append(f"• {row['title']}")
            lines.append(
                f"  Status: {_format_commitment_status(row['status'])}"
                f" · Owner: {_format_commitment_owner(row['owner_role'])}"
            )
            lines.append("")
        details = "\n".join(lines).rstrip()
        if llm_details and not any(row["title"].lower() in llm_details.lower() for row in rows[:2]):
            details = f"{details}\n\n{llm_details}"

    return {
        "employee_count": employee_count,
        "leader_count": leader_count,
        "open_count": open_count,
        "items": [
            f"Employee Commitments: {employee_count} active",
            f"Leader Commitments: {leader_count} active",
            f"Open Commitments: {open_count} total",
        ],
        "details": details,
    }


def _normalize_brief(data: Dict[str, Any]) -> Dict[str, Any]:
    return {key: _section_block(data.get(key)) for key in BRIEF_SECTION_KEYS}


def _normalize_synthesis(data: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in SYNTHESIS_SECTION_KEYS:
        block = data.get(key)
        if key == "alignment_summary" and isinstance(block, dict):
            score = block.get("score")
            try:
                score_val = float(score) if score is not None and score != "" else None
            except (TypeError, ValueError):
                score_val = None
            if score_val is not None:
                score_val = max(1.0, min(5.0, score_val))
            normalized = _section_block(block)
            normalized["score"] = score_val
            normalized["label"] = str(block.get("label") or "").strip() or (
                "Aligned" if score_val and score_val >= 4.0 else "Building alignment"
            )
            out[key] = normalized
            continue
        if key == "commitment_summary" and isinstance(block, dict):
            normalized = _section_block(block, max_items=8)
            for count_key in ("employee_count", "leader_count", "open_count"):
                try:
                    normalized[count_key] = int(block.get(count_key) or 0)
                except (TypeError, ValueError):
                    normalized[count_key] = 0
            out[key] = normalized
            continue
        out[key] = _section_block(block)
    return out


def _load_one_on_one_knowledge() -> str:
    try:
        db = get_vector_store()
        chunks = get_chunks_by_category(db, ONE_ON_ONE_KNOWLEDGE_CATEGORY)
        if chunks:
            return "\n\n---\n\n".join(chunks[:12])
    except Exception as err:
        logger.warning("Could not load 1-on-1 knowledge from ChromaDB: %s", err)
    return ""


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
        logger.error("Failed to parse one-on-one LLM JSON: %s | raw=%s", err, content[:500])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI returned invalid JSON. Please retry.",
        ) from err

    return parsed, model, prompt_tokens, completion_tokens, total_tokens


@router.post("/meeting-brief", response_model=MeetingBriefResponse)
async def meeting_brief(payload: MeetingBriefRequest) -> MeetingBriefResponse:
    """
    Generate AI Meeting Brief from Step 1 continuous evidence + prior syntheses.
    Called by Laravel OneOnOneAiService.
    """
    model = resolve_insight_model(payload.model)
    knowledge = _load_one_on_one_knowledge()
    custom_system = (payload.system_prompt or "").strip()
    system_prompt = custom_system if custom_system else _load_prompt(
        "one_on_one_brief_system.md",
        "Generate a JSON meeting brief with alignment_snapshot, development_snapshot, commitment_review, "
        "behavioral_trends, suggested_discussion_areas, emerging_opportunities, potential_barriers.",
    )

    user_payload = {
        "conversation_id": payload.conversation_id,
        "leader_user_id": payload.leader_user_id,
        "employee_user_id": payload.employee_user_id,
        "prior_syntheses": payload.prior_syntheses,
        "evidence_context": payload.evidence_context,
    }
    user_prompt = (
        "FUSION 1-on-1 Framework knowledge (category fusion_one_on_one):\n"
        f"{knowledge or '(no indexed knowledge yet)'}\n\n"
        "Generate the meeting brief JSON from this payload:\n"
        f"{json.dumps(user_payload, ensure_ascii=False, indent=2)}"
    )

    parsed, used_model, prompt_tokens, completion_tokens, total_tokens = _run_json_completion(
        system_prompt, user_prompt, model
    )
    brief = _normalize_brief(parsed)
    cost = estimate_cost_usd(used_model, prompt_tokens, completion_tokens)

    return MeetingBriefResponse(
        brief=brief,
        model=used_model,
        tokens_used=total_tokens,
        cost_usd=cost,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prompt_version_id=payload.prompt_version_id,
        prompt_version_label=payload.prompt_version_label,
    )


@router.post("/meeting-synthesis", response_model=MeetingSynthesisResponse)
async def meeting_synthesis(payload: MeetingSynthesisRequest) -> MeetingSynthesisResponse:
    """
    Generate AI Meeting Synthesis after a 1-on-1 conversation is completed.
    Uses current conversation preparations, notes, and commitments only.
    """
    model = resolve_insight_model(payload.model)
    knowledge = _load_one_on_one_knowledge()
    custom_system = (payload.system_prompt or "").strip()
    system_prompt = custom_system if custom_system else _load_prompt(
        "one_on_one_synthesis_system.md",
        "Generate a JSON meeting synthesis with meeting_summary, alignment_summary, development_summary, "
        "commitment_summary, emerging_risks, emerging_opportunities, suggested_coaching_topics, "
        "recommended_follow_up.",
    )

    user_payload = {
        "conversation_id": payload.conversation_id,
        "leader_user_id": payload.leader_user_id,
        "employee_user_id": payload.employee_user_id,
        "preparations": payload.preparations,
        "notes": payload.notes,
        "commitments": payload.commitments,
    }
    user_prompt = (
        "FUSION 1-on-1 Framework knowledge (category fusion_one_on_one):\n"
        f"{knowledge or '(no indexed knowledge yet)'}\n\n"
        "Generate the meeting synthesis JSON from this payload:\n"
        f"{json.dumps(user_payload, ensure_ascii=False, indent=2)}"
    )

    parsed, used_model, prompt_tokens, completion_tokens, total_tokens = _run_json_completion(
        system_prompt, user_prompt, model
    )
    synthesis = _normalize_synthesis(parsed)
    synthesis["commitment_summary"] = _commitment_summary_from_rows(
        payload.commitments,
        synthesis.get("commitment_summary"),
    )
    cost = estimate_cost_usd(used_model, prompt_tokens, completion_tokens)

    return MeetingSynthesisResponse(
        synthesis=synthesis,
        model=used_model,
        tokens_used=total_tokens,
        cost_usd=cost,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prompt_version_id=payload.prompt_version_id,
        prompt_version_label=payload.prompt_version_label,
    )
