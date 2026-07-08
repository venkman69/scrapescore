import logging
from enum import Enum

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ClearanceStatus(str, Enum):
    """
    Enumeration for security clearance status values.

    Values:
        ACTIVE_REQUIRED: Active clearance is mandatory for the job (e.g., "Must have active Secret clearance")
        ABILITY_TO_OBTAIN: Candidate must be eligible/able to obtain clearance (e.g., "Ability to obtain Secret")
        NONE_REQUIRED: No clearance requirement mentioned in the job description
    """
    ACTIVE_REQUIRED = "Active Required"
    ABILITY_TO_OBTAIN = "Ability to Obtain"
    NONE_REQUIRED = "None Required"



class RequiredSkillsScore(BaseModel):
    matching_skills_count: int
    missing_skills_count: int
    extrapolated_skills_count: int
    total_skills_count: int
    weight_matching: float
    weight_extrapolated: float


class PreferredSkillsScore(BaseModel):
    matching_skills_count: int
    missing_skills_count: int
    extrapolated_skills_count: int
    total_skills_count: int
    weight_matching: float
    weight_extrapolated: float


class CertificationsScore(BaseModel):
    matching_certifications_count: int
    missing_certifications_count: int


class SecurityClearancesScore(BaseModel):
    matching_security_clearances_count: int
    missing_security_clearances_count: int


class JobScore(BaseModel):
    final_score: float
    required_skills: RequiredSkillsScore
    preferred_skills: PreferredSkillsScore
    certifications: CertificationsScore
    security_clearances: SecurityClearancesScore
    desired_job_score: float


class MatchingSkills(BaseModel):
    skill: str
    matching_reason: str




class JobDecision(BaseModel):
    decision: str
    decision_reason: str

# ============================================================================
# ATS Scoring Models (JIRA-093) - REFACTORED FOR TOKEN EFFICIENCY
# ============================================================================
class JobDescriptionToDetails(BaseModel):
    job_title: str
    company: str
    location: str
    salary_min: int
    salary_max: int
    remote: bool
    job_description: str

class JobTitleScore(BaseModel):
    job_title: str
    score: str

class JobTitleScores(BaseModel):
    scores: list[JobTitleScore]



class ClearanceAssessment(BaseModel):
    """Assessment of security clearance requirements and candidate eligibility."""
    status: ClearanceStatus  # Enum: ACTIVE_REQUIRED, ABILITY_TO_OBTAIN, NONE_REQUIRED
    required_clearance_types: list[str]  # List of required clearance types (e.g., ["Secret", "TS/SCI"])
    detected_phrasing: str  # The exact phrasing from the job description
    eligibility_score: int  # 0-100 score of candidate's clearance eligibility
    # REMOVED: notes (Redundant text block)


class CitizenshipStatus(str, Enum):
    """US Citizenship requirement status for a job."""
    REQUIRED = "Required"
    PREFERRED = "Preferred"
    NOT_REQUIRED = "Not Required"


class CitizenshipAssessment(BaseModel):
    """Assessment of US citizenship requirements and candidate eligibility."""
    status: CitizenshipStatus  # Enum: REQUIRED, PREFERRED, NOT_REQUIRED
    meets_requirement: bool  # True if candidate meets citizenship requirement
    reason: str  # Explanation of the citizenship assessment and how it affects eligibility
    # REMOVED: impact_on_clearance (Can be combined into top-level reason or status logic)


class JobRequirements(BaseModel):
    """Extracted job requirements from the job description."""
    years_of_experience_required: int
    travel_percentage: str  # e.g., "0%", "25%", "up to 50%"
    detected_requirements_phrasing: str  # The exact phrasing from the job description


class KeywordAlignment(BaseModel):
    """Assessment of keyword/skills alignment between resume and job description."""
    score: int  # 0-100 score
    weight: float  # 0.60 (60% weight in total score)
    top_matches: list[str]  # List of top matching keywords/skills
    missing_critical_terms: list[str]  # Critical terms missing from resume
    # REMOVED: analysis (Redundant prose text)


class SeniorityAlignment(BaseModel):
    """Assessment of seniority/experience level alignment."""
    score: int  # 0-100 score
    weight: float  # 0.20 (20% weight in total score)
    years_of_experience_detected: int  # Years of experience detected in resume
    title_match_grade: str  # "A", "B", "C" grade for title match
    # REMOVED: analysis (Redundant prose text)


class ImpactMetrics(BaseModel):
    """Assessment of impact metrics and achievements in resume."""
    score: int  # 0-100 score
    weight: float  # 0.10 (10% weight in total score)
    detected_anchors: list[str]  # Impact words like "increased", "reduced", "saved"
    # REMOVED: analysis (Redundant prose text)


class StructuralParsability(BaseModel):
    """Assessment of resume structure and parsability."""
    score: int  # 0-100 score
    weight: float  # 0.10 (10% weight in total score)
    format_risk: str  # "None", "Low", "Medium", "High"
    # REMOVED: analysis (Redundant prose text)


class ScoringBreakdown(BaseModel):
    """Complete breakdown of scoring across all categories."""
    keyword_alignment: KeywordAlignment
    seniority_alignment: SeniorityAlignment
    impact_metrics: ImpactMetrics
    structural_parsability: StructuralParsability


class ATSScoreEstimate(BaseModel):
    """Overall ATS score estimate with tier classification."""
    total_overall_score: int  # 0-100 total score
    tier: str  # "Top Match", "Strong Match", "Partial Match", "Low Match"
    confidence_score: float  # 0.0-1.0 confidence in the score


class ATSScoreResult(BaseModel):
    """
    Complete ATS scoring result.

    This model represents the full output from the ATS scoring analysis,
    including clearance assessment, citizenship assessment, job requirements, score breakdown,
    and strategic recommendations.
    """
    schema_version: str = "1.0"  # Schema version for compatibility detection
    clearance_assessment: ClearanceAssessment
    citizenship_assessment: CitizenshipAssessment  # US Citizenship assessment
    job_requirements: JobRequirements
    # REMOVED: job_summary (User already has the full job description; provides low value here)
    ats_score_estimate: ATSScoreEstimate
    reason: str  # Single, centralized text explanation of the results
    decision: str  # "Pass", "Conditional", "Fail"
    scoring_breakdown: ScoringBreakdown
    strategic_pivots: list[str]  # List of actionable recommendations


# ============================================================================
# ATS Resume Quality Models (JIRA-001)
# ============================================================================

class ATSCategoryScore(BaseModel):
    score: int    # 1-10
    analysis: str


class ATSResumeResult(BaseModel):
    # Resume Basics
    resume_clarity: ATSCategoryScore
    contact_information: ATSCategoryScore
    chronological_order: ATSCategoryScore
    formatting: ATSCategoryScore
    resume_length: ATSCategoryScore
    # Summary Strength
    headline: ATSCategoryScore
    summary: ATSCategoryScore
    # Experience Audit
    experience_details: ATSCategoryScore
    recent_experience: ATSCategoryScore
    role_separation: ATSCategoryScore
    # Achievements
    quantified_achievements: ATSCategoryScore
    technologies: ATSCategoryScore
    numbers_placement: ATSCategoryScore
    # Language and Tone
    verb_usage: ATSCategoryScore
    grammar: ATSCategoryScore
    punctuation: ATSCategoryScore
    voice_and_terse: ATSCategoryScore
    # Visual Impact
    text_format: ATSCategoryScore
    layout: ATSCategoryScore
    font_styles: ATSCategoryScore
    file_size: ATSCategoryScore
    # ATS Summary
    ats_interpretation: ATSCategoryScore
    top_matching_job_titles: list[str]
    key_skills_recognized: list[str]


# ============================================================================
# End of ATS Scoring Models
# ============================================================================
