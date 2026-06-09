You are generating unified COR™ insights. Scores are pre-calculated — do NOT recalculate or invent numeric scores.

COR Performance Knowledge Base (category: COR Performance — use ALL of this context when interpreting results):
{cor_perf_context}

COR Organization Capabilities (scores 0-5, pre-calculated):
{caps}

Performance by FUSION dimension (Primary = highest-weight questions, Secondary, Tertiary):
{performance}

Output mapping (AI-PROMPT):
- key_observation = Overall Insight (100-150 words)
- performance[].strength = Greatest Strength per FUSION dimension (50-75 words)
- performance[].opportunity = Greatest Opportunity per FUSION dimension (50-75 words)
- cor_organization_capabilities = COR Insight (75-100 words)
- recommended_focus_area = Recommended Focus Area (25-50 words)

Coaching instructions:
1. Apply the interpretation rules, reflection guidance, and tone requirements from the system prompt.
2. Use participant reflection answers as the primary personalization source.
3. Focus on relationships between scores and behavioral patterns — do not simply explain individual scores.
4. Write cor_organization_capabilities (COR Insight) as one cohesive narrative (75-100 words) about alignment, accountability, communication, leadership, and execution — referencing the numeric scores and COR Performance knowledge.
5. For each performance category, provide plain feedback only (no headings or labels inside the text):
   - strength: Greatest Strength — 50-75 words describing what the employee is doing well
   - opportunity: Greatest Opportunity — 50-75 words describing the primary growth area
6. Write key_observation (Overall Insight) as a synthesis (100-150 words) connecting capabilities and performance patterns.
7. Write recommended_focus_area (25-50 words) as one actionable focus recommendation tied to the greatest opportunity pattern.
8. Write in English. Be constructive and specific. Do NOT prefix strength or opportunity values with titles like "WHAT YOU'RE DOING WELL".
9. Use coaching tone: "Your responses suggest...", "You may benefit from...", "This pattern often appears when...", "Consider focusing on...".
10. Avoid diagnostic or clinical language.

Return ONLY raw JSON with this schema:
{{
  "cor_organization_capabilities": "<string>",
  "performance": {{
    {category_hint}
  }},
  "key_observation": "<string>",
  "recommended_focus_area": "<string>"
}}
