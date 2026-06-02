# Goal:
Review a list of job titles and determine how well it matches the desired role description.
Score the job title as high, medium or low based on the match.
# Inputs:
* Job Titles: 
--- START OF JOB TITLES ---
{job_titles}
--- END OF JOB TITLES ---
* Desired Role Description: 
--- START OF DESIRED ROLE DESCRIPTION ---
{desired_role_description}
--- END OF DESIRED ROLE DESCRIPTION ---
expected_output: the output MUST be just the JSON object and it MUST follow the pydantic model below:
```python
class JobTitleScore(BaseModel):
    job_title: str
    score: str


class JobTitleScores(BaseModel):
    scores: list[JobTitleScore]
```
# Example:
```json
[
{{
  "job_title": "Job Title 1",
  "score": "high"
}},
{{
  "job_title": "Job Title 2",
  "score": "medium"
}},
{{
  "job_title": "Job Title 3",
  "score": "low"
}},
]
```
