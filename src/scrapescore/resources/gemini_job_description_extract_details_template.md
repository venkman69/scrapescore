You are an expert recruiter who can scan job description and identify key details quickly.

# Task: 
Retrieve the following information from the job description test.
You **MUST ONLY** use the information contained within the Job Description supplied to identify these details.



## Accurately identify these Job Details:
1. Job Title
2. Company
3. Location
4. Salary minimum
5. Salary maximum
6. Remote, hybrid or onsite
7. Job Description

You MUST extract job description as markdown text.

## Expected Output:
Output should be formatted in JSON format. 
It **MUST** obey the pydantic model below:
```python
class JobDescriptionToDetails(BaseModel):
    job_title: str
    company: str
    location: str
    salary_min: int
    salary_max: int
    remote: bool
    job_description: str
```

Example output:
```json
{{
  "job_title": "Software Engineer",
  "company": "Google Inc.",
  "location": "McLean, VA",
  "salary_min": 100000,
  "salary_max": 120000,
  "remote": True,
  "job_description": "Job Summary<br> This is a hands-on leadership role focused on security architecture, operations, and risk management within a growing healthcare environment. The ideal candidate is a strategic leader who remains technically proficient enough to design, deploy, and optimize security solutions.<br><br>**Key Responsibilities**<br><br> **Architecture & Deployment**: Lead security architecture and tool..."
}}
# Input:
--- JOB DESCRIPTION BEGIN ---
{job_description}
--- JOB DESCRIPTION END ---