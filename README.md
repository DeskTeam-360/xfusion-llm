# Xfusion Exam Evaluation API

An automated employee exam evaluation API designed to act as an external service for WordPress sites. Built using **FastAPI**, **LangChain**, **OpenAI (GPT-4o-mini & Text-Embedding-3-Small)**, and **ChromaDB**.

## Features
- **FastAPI Core**: Highly performant, auto-generates Swagger & Redoc interactive API documentation.
- **Secure Authentication**: Uses API Key Bearer Authentication (`Authorization: Bearer <your_key>`).
- **Vector Database (ChromaDB)**: Persisted in `./chroma_db` for fast and context-rich semantic searches of company knowledge.
- **Deduplication Logic**: Automatically purges older chunk copies on upserting WordPress posts to prevent database bloat and duplicate contexts.
- **Advanced AI Grading**: Triggers `gpt-4o-mini` with forced structured JSON schema output to retrieve flawless evaluation cards (Score, Strengths, Improvements, and HR Feedback) in English.

---

## Project Structure
```
├── .env.example
├── .env                  # Environment keys
├── requirements.txt      # PyPI packages
├── main.py               # App entrypoint and configuration
├── config.py             # Pydantic environment validation settings
├── database.py           # ChromaDB & OpenAI Embeddings setup
├── security.py           # HTTPBearer authentication middleware
└── routers/
    ├── __init__.py
    ├── knowledge.py      # CRUD Knowledge Base endpoints
    └── evaluation.py     # Evaluation and LLM prompt logic
```

---

## Getting Started

### 1. Prerequisite Checklist
Make sure you have Python 3.9+ installed on your system.

### 2. Installation
Clone/copy the project files to your server or local machine, navigate to the directory, and set up a virtual environment:

```bash
# Create virtual environment
python -m venv venv

# Activate virtual environment
# On Windows (PowerShell):
.\venv\Scripts\Activate.ps1
# On Windows (CMD):
.\venv\Scripts\activate.bat
# On Linux/macOS:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Environment Configuration
Open the generated `.env` file and configure your credentials:

```ini
# App Configuration
HOST=0.0.0.0
PORT=8000
API_KEY=super_secret_wordpress_token

# OpenAI Configuration
OPENAI_API_KEY=sk-proj-YourOpenAIApiKeyGoesHere

# Chroma Database Configuration
CHROMA_PERSIST_DIR=./chroma_db
```

### 4. Running the Server
Run the FastAPI development server directly:

```bash
python main.py
```
Or run using `uvicorn` CLI:
```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Once running, navigate to `http://localhost:8000/docs` to see the **interactive Swagger documentation**!

### 5. Testing with Postman
We have included a pre-configured Postman Collection file in the root of the project folder:
`xfusion_exam_evaluation_api.postman_collection.json`

To start testing:
1. Open **Postman**.
2. Click **Import** in the top-left corner.
3. Drag and drop the `xfusion_exam_evaluation_api.postman_collection.json` file or click to select it.
4. Once imported, select the **Xfusion Exam Evaluation API** collection in the sidebar.
5. Navigate to the **Variables** tab of the collection to customize:
   - `baseUrl`: The address of your running FastAPI service (defaults to `http://localhost:8000`).
   - `apiKey`: Your secure authorization token (defaults to `super_secret_wordpress_token` to match the default `.env`).
6. Click **Save**, and now all requests inside the collection are fully ready to run with a single click!

---

## API Endpoints & WordPress Integration Guides

All requests must contain the following header:
`Authorization: Bearer <YOUR_API_KEY>`

### A. Upsert Knowledge Base (`POST /api/v1/knowledge/upsert`)
Triggered when a WordPress Post or Page containing company policies/SOPs is created or updated.

**cURL Example**:
```bash
curl -X 'POST' \
  'http://localhost:8000/api/v1/knowledge/upsert' \
  -H 'accept: application/json' \
  -H 'Authorization: Bearer super_secret_wordpress_token' \
  -H 'Content-Type: application/json' \
  -d '{
  "wordpress_post_id": 101,
  "category": "Customer Service",
  "content": "SOP for Handling Customer Complaints: 1. Always listen to customer complaints calmly without interrupting. 2. Apologize for the inconvenience they experienced. 3. Provide solution options in accordance with the company'\''s 30-day warranty policy. 4. If the customer is still unsatisfied, offer to escalate the issue to the Team Supervisor within 1 hour."
}'
```

---

### B. List Knowledge Base (`GET /api/v1/knowledge/list`)
Allows you to retrieve and review all currently stored document chunks and metadata inside ChromaDB. Great for validating what is inside the vector database!

**cURL Example**:
```bash
curl -X 'GET' \
  'http://localhost:8000/api/v1/knowledge/list' \
  -H 'accept: application/json'
```

---

### C. Delete Knowledge Base (`DELETE /api/v1/knowledge/delete/{post_id}`)
Triggered when a WordPress Post or Page containing company policies is deleted.

**cURL Example**:
```bash
curl -X 'DELETE' \
  'http://localhost:8000/api/v1/knowledge/delete/101' \
  -H 'accept: application/json' \
  -H 'Authorization: Bearer super_secret_wordpress_token'
```

---

### C. Evaluate Exam Answer (`POST /api/v1/evaluation/evaluate`)
Triggered when an employee submits their exam answers from WordPress.

**cURL Example**:
```bash
curl -X 'POST' \
  'http://localhost:8000/api/v1/evaluation/evaluate' \
  -H 'accept: application/json' \
  -H 'Authorization: Bearer super_secret_wordpress_token' \
  -H 'Content-Type: application/json' \
  -d '{
  "user_id": 42,
  "exam_id": 5,
  "answers_data": [
    {
      "category": "Customer Service",
      "question_answers": [
        {
          "question": "How do you handle an angry customer?",
          "answer": "I listen to their complaint calmly and apologize. Afterward, I solve it myself immediately without involving the supervisor."
        }
      ]
    }
  ]
}'
```

**Response Output (Example)**:
```json
{
  "user_id": 42,
  "exam_id": 5,
  "evaluations": [
    {
      "category": "Customer Service",
      "score": 75,
      "strengths": "The employee understands the importance of listening calmly and apologizing for the inconvenience.",
      "improvements": "The employee stated they would handle everything themselves without involving the supervisor, which contradicts SOP point 4 where they must offer an escalation to the Team Supervisor within 1 hour if the customer remains unsatisfied.",
      "evaluator_notes": "Basic empathy is excellent. However, please review the escalation protocol to supervisor when a direct solution does not satisfy the customer."
    }
  ]
}
```

---

## Implementation Details & Security Recommendations
1. **API Key Strength**: Use a long, cryptographically secure random key (e.g., generated with `openssl rand -hex 32`) for `API_KEY`.
2. **CORS Configuration**: In `main.py`, restrict `allow_origins=["*"]` to your exact WordPress domain (e.g., `allow_origins=["https://yourwordpresssite.com"]`) before deployment to production.
3. **Database Backups**: Regularly backup your `./chroma_db` directory to safeguard persistent company vectors.

