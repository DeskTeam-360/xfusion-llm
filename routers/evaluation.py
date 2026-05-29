import json
import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from database import get_vector_store, get_chunks_by_wordpress_post_id
from config import settings

# Configure logging
logger = logging.getLogger("xfusion-backend.routers.evaluation")

router = APIRouter(
    prefix="/api/v1/evaluation",
    tags=["Exam Evaluation"]
    # dependencies=[Depends(verify_api_key)]  # Temporarily disabled for testing
)

# Request Models
class QuestionAnswer(BaseModel):
    question: str = Field(..., description="The exam question", example="How should you greet a customer?")
    answer: str = Field(..., description="The answer submitted by the exam taker", example="Greet with a warm smile and eye contact.")


class EvaluationRequest(BaseModel):
    user_id: int = Field(..., description="ID of the user / employee", example=42)
    created_at: Optional[datetime] = Field(
        None,
        description="Submission or session timestamp from the client (ISO 8601). Used to track when this evaluation was requested.",
        example="2026-05-29T10:30:00Z",
    )
    company_information: int = Field(
        ...,
        description="WordPress post ID of the indexed company knowledge document",
        example=101,
    )
    question_answers: List[QuestionAnswer] = Field(
        ...,
        description="List of exam questions and employee answers",
    )


# Response Models
class EvaluationResult(BaseModel):
    score: int = Field(..., description="Evaluation score from 0 to 100")
    strengths: str = Field(..., description="What the candidate did right / matched company standards")
    improvements: str = Field(..., description="What the candidate lacked / got wrong according to company database")
    evaluator_notes: str = Field(..., description="Concise feedback/notes from the evaluator")


class EvaluationResponse(BaseModel):
    user_id: int = Field(..., description="ID of the evaluated employee")
    created_at: datetime = Field(..., description="Client submission time or server time when the request was received")
    evaluated_at: datetime = Field(..., description="Server UTC timestamp when evaluation completed (last API call)")
    company_information: int = Field(..., description="WordPress post ID used for company knowledge context")
    evaluation: EvaluationResult = Field(..., description="Structured evaluation result")


@router.post("/evaluate", response_model=EvaluationResponse, status_code=status.HTTP_200_OK)
async def evaluate_exam(payload: EvaluationRequest):
    """
    Evaluates an employee's exam answers against company knowledge indexed by WordPress post ID.

    1. Loads all ChromaDB chunks for `company_information` (post_id).
    2. Builds an HR evaluation prompt with that context.
    3. Runs gpt-4o-mini and returns score + feedback in English.
    4. Echoes `created_at` and sets `evaluated_at` for last-call tracking.
    """
    request_received_at = datetime.now(timezone.utc)
    created_at = payload.created_at
    if created_at is not None and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    if created_at is None:
        created_at = request_received_at

    try:
        db = get_vector_store()

        if not settings.OPENAI_API_KEY or settings.OPENAI_API_KEY.strip() == "" or settings.OPENAI_API_KEY.startswith("sk-proj-..."):
            raise ValueError("OPENAI_API_KEY is not configured or is using a placeholder. Please configure it in your .env file.")

        llm = ChatOpenAI(
            model="gpt-4o-mini",
            openai_api_key=settings.OPENAI_API_KEY,
            temperature=0.2,
            model_kwargs={"response_format": {"type": "json_object"}},
        )

        post_id = payload.company_information
        logger.info(
            f"Processing evaluation for user_id={payload.user_id}, "
            f"company_information (post_id)={post_id}, created_at={created_at.isoformat()}"
        )

        chunk_texts = get_chunks_by_wordpress_post_id(db, post_id)
        if chunk_texts:
            context_str = "\n---\n".join(chunk_texts)
            logger.info(f"Retrieved {len(chunk_texts)} knowledge chunks for post_id {post_id}")
        else:
            context_str = "NO SPECIFIC COMPANY CONTEXT FOUND IN VECTOR STORE FOR THIS POST ID."
            logger.info(f"No knowledge chunks found for post_id {post_id}")

        qa_list = []
        for idx, qa in enumerate(payload.question_answers, 1):
            qa_list.append(f"Question {idx}: {qa.question}\nEmployee Answer: {qa.answer}")
        qa_data_str = "\n\n".join(qa_list)

        prompt_template = ChatPromptTemplate.from_messages([
            ("system", "You are a professional, highly objective, and detail-oriented HR (Human Resources) Evaluator."),
            ("user", """
Your task is to objectively evaluate an employee's exam answers based on the Company Knowledge Context provided below.

Company Knowledge Context (WordPress post ID: {post_id}):
{context}

Employee Exam — Questions & Answers:
{qa_data}

Evaluation Instructions:
1. Compare the employee's answers directly with the provided Company Knowledge Context.
2. Determine what the employee answered CORRECTLY according to the context.
3. Determine what the employee answered INCORRECTLY or MISSED/FAILED to mention according to the context.
4. Provide an objective score between 0 and 100 based on the employee's level of understanding.
   - If no Company Knowledge Context is found (NO SPECIFIC COMPANY CONTEXT FOUND), evaluate based on general professional standards and include a note that no specific SOP was found in the database for this post ID.
5. Write the evaluation response in English.

Return the response ONLY in raw JSON format with the following schema:
{{
    "score": <integer_from_0_to_100>,
    "strengths": "<summary_of_what_was_correct_and_in_line_with_company_context>",
    "improvements": "<summary_of_what_was_incorrect_or_missing_according_to_company_context>",
    "evaluator_notes": "<concise_and_constructive_feedback_for_the_employee>"
}}
"""),
        ])

        evaluation_result: EvaluationResult
        try:
            chain = prompt_template | llm
            response = await chain.ainvoke({
                "post_id": post_id,
                "context": context_str,
                "qa_data": qa_data_str,
            })
            eval_data = json.loads(response.content)
            evaluation_result = EvaluationResult(
                score=int(eval_data.get("score", 0)),
                strengths=eval_data.get("strengths", "No feedback provided."),
                improvements=eval_data.get("improvements", "No feedback provided."),
                evaluator_notes=eval_data.get("evaluator_notes", "No feedback provided."),
            )
        except json.JSONDecodeError:
            logger.error(f"Failed to decode LLM response as JSON: {response.content}")
            evaluation_result = EvaluationResult(
                score=0,
                strengths="Failed to process evaluation score.",
                improvements="Invalid LLM output formatting.",
                evaluator_notes="An error occurred while parsing the AI evaluator response.",
            )
        except Exception as llm_err:
            logger.error(f"Error calling LLM for post_id {post_id}: {str(llm_err)}")
            evaluation_result = EvaluationResult(
                score=0,
                strengths="Failed to evaluate.",
                improvements=f"Internal LLM Error: {str(llm_err)}",
                evaluator_notes="Please contact system administrator for technical details.",
            )

        evaluated_at = datetime.now(timezone.utc)
        logger.info(
            f"Evaluation completed for user_id={payload.user_id}, post_id={post_id}, "
            f"created_at={created_at.isoformat()}, evaluated_at={evaluated_at.isoformat()}"
        )

        return EvaluationResponse(
            user_id=payload.user_id,
            created_at=created_at,
            evaluated_at=evaluated_at,
            company_information=post_id,
            evaluation=evaluation_result,
        )

    except ValueError as val_err:
        logger.error(f"Validation or configuration error in evaluation: {str(val_err)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(val_err),
        )
    except Exception as e:
        logger.error(f"Unexpected error in evaluation: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred during evaluation: {str(e)}",
        )
