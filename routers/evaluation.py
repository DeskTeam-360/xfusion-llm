import json
import logging
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from database import get_vector_store
from security import verify_api_key
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

class CategoryAnswers(BaseModel):
    category: str = Field(..., description="Category group of the questions", example="Customer Service")
    question_answers: List[QuestionAnswer] = Field(..., description="List of questions and answers inside this category")

class EvaluationRequest(BaseModel):
    user_id: int = Field(..., description="ID of the user / employee", example=42)
    exam_id: int = Field(..., description="ID of the exam", example=12)
    answers_data: List[CategoryAnswers] = Field(..., description="Categories containing the questions and answers")

# Response Models
class CategoryEvaluationResult(BaseModel):
    category: str = Field(..., description="Category evaluated")
    score: int = Field(..., description="Evaluation score from 0 to 100")
    strengths: str = Field(..., description="What the candidate did right / matched company standards")
    improvements: str = Field(..., description="What the candidate lacked / got wrong according to company database")
    evaluator_notes: str = Field(..., description="Concise feedback/notes from the evaluator")

class EvaluationResponse(BaseModel):
    user_id: int = Field(..., description="ID of the evaluated employee")
    exam_id: int = Field(..., description="ID of the evaluated exam")
    evaluations: List[CategoryEvaluationResult] = Field(..., description="Evaluation details for each category")


@router.post("/evaluate", response_model=EvaluationResponse, status_code=status.HTTP_200_OK)
async def evaluate_exam(payload: EvaluationRequest):
    """
    Evaluates an employee's exam answers against company knowledge saved in ChromaDB.
    
    For each category in the exam:
    1. Performs a similarity search in ChromaDB using category name to retrieve the top 3 relevant policy documents.
    2. Builds a comprehensive, professional HR evaluation prompt using ChatPromptTemplate.
    3. Triggers OpenAI gpt-4o-mini to grade, identify strengths/weaknesses, and write feedback in English.
    4. Returns an aggregated structured response.
    """
    try:
        # 1. Initialize DB and LLM
        db = get_vector_store()
        
        if not settings.OPENAI_API_KEY or settings.OPENAI_API_KEY.strip() == "" or settings.OPENAI_API_KEY.startswith("sk-proj-..."):
            raise ValueError("OPENAI_API_KEY is not configured or is using a placeholder. Please configure it in your .env file.")
            
        llm = ChatOpenAI(
            model="gpt-4o-mini",
            openai_api_key=settings.OPENAI_API_KEY,
            temperature=0.2,
            model_kwargs={"response_format": {"type": "json_object"}}
        )
        
        # 2. Build standard System / User ChatPromptTemplate in English
        prompt_template = ChatPromptTemplate.from_messages([
            ("system", "You are a professional, highly objective, and detail-oriented HR (Human Resources) Evaluator."),
            ("user", """
Your task is to objectively evaluate an employee's exam answers based on the Company Knowledge Context provided below.

Company Knowledge Context:
{context}

Employee Exam for Category: "{category}"
Questions & Answers:
{qa_data}

Evaluation Instructions:
1. Compare the employee's answers directly with the provided Company Knowledge Context.
2. Determine what the employee answered CORRECTLY according to the context.
3. Determine what the employee answered INCORRECTLY or MISSED/FAILED to mention according to the context.
4. Provide an objective score between 0 and 100 based on the employee's level of understanding.
   - If no Company Knowledge Context is found (NO SPECIFIC COMPANY CONTEXT FOUND), evaluate based on general professional standards and include a note that no specific SOP was found in the database.
5. Write the evaluation response in English.

Return the response ONLY in raw JSON format with the following schema:
{{
    "score": <integer_from_0_to_100>,
    "strengths": "<summary_of_what_was_correct_and_in_line_with_company_context>",
    "improvements": "<summary_of_what_was_incorrect_or_missing_according_to_company_context>",
    "evaluator_notes": "<concise_and_constructive_feedback_for_the_employee>"
}}
""")
        ])

        evaluation_results = []
        
        # 3. Process each category
        for item in payload.answers_data:
            logger.info(f"Processing evaluation for category: '{item.category}'")
            
            # Retrieve relevant context from ChromaDB
            # We first try to fetch with a category filter for absolute precision
            context_docs = []
            try:
                context_docs = db.similarity_search(
                    query=item.category,
                    k=3,
                    filter={"category": item.category}
                )
            except Exception as filter_err:
                logger.warning(f"Similarity search with category filter failed, attempting default search: {str(filter_err)}")
                
            # If no category filter results found, perform standard similarity search
            if not context_docs:
                try:
                    context_docs = db.similarity_search(query=item.category, k=3)
                except Exception as search_err:
                    logger.error(f"Global similarity search failed: {str(search_err)}")
            
            # Format context string
            if context_docs:
                context_str = "\n---\n".join([doc.page_content for doc in context_docs])
                logger.info(f"Retrieved {len(context_docs)} reference documents for category '{item.category}'")
            else:
                context_str = "NO SPECIFIC COMPANY CONTEXT FOUND IN VECTOR STORE FOR THIS CATEGORY."
                logger.info(f"No reference documents found in database for category '{item.category}'")

            # Format Q&A data for the prompt
            qa_list = []
            for idx, qa in enumerate(item.question_answers, 1):
                qa_list.append(f"Question {idx}: {qa.question}\nEmployee Answer: {qa.answer}")
            qa_data_str = "\n\n".join(qa_list)

            # 4. Invoke LLM Chain
            try:
                chain = prompt_template | llm
                response = await chain.ainvoke({
                    "context": context_str,
                    "category": item.category,
                    "qa_data": qa_data_str
                })
                
                # Parse LLM response
                eval_data = json.loads(response.content)
                
                evaluation_results.append(
                    CategoryEvaluationResult(
                        category=item.category,
                        score=int(eval_data.get("score", 0)),
                        strengths=eval_data.get("strengths", "No feedback provided."),
                        improvements=eval_data.get("improvements", "No feedback provided."),
                        evaluator_notes=eval_data.get("evaluator_notes", "No feedback provided.")
                    )
                )
            except json.JSONDecodeError as decode_err:
                logger.error(f"Failed to decode LLM response as JSON: {response.content}")
                evaluation_results.append(
                    CategoryEvaluationResult(
                        category=item.category,
                        score=0,
                        strengths="Failed to process evaluation score.",
                        improvements="Invalid LLM output formatting.",
                        evaluator_notes="An error occurred while parsing the AI evaluator response."
                    )
                )
            except Exception as llm_err:
                logger.error(f"Error calling LLM for category {item.category}: {str(llm_err)}")
                evaluation_results.append(
                    CategoryEvaluationResult(
                        category=item.category,
                        score=0,
                        strengths="Failed to evaluate.",
                        improvements=f"Internal LLM Error: {str(llm_err)}",
                        evaluator_notes="Please contact system administrator for technical details."
                    )
                )
                
        return EvaluationResponse(
            user_id=payload.user_id,
            exam_id=payload.exam_id,
            evaluations=evaluation_results
        )

    except ValueError as val_err:
        logger.error(f"Validation or configuration error in evaluation: {str(val_err)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(val_err)
        )
    except Exception as e:
        logger.error(f"Unexpected error in evaluation: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred during evaluation: {str(e)}"
        )
