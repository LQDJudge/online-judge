"""
Problem tagging package for LQDOJ
Provides AI-powered problem difficulty and tag prediction
"""

from .problem_tagger import ProblemTagger
from .problem_tag_service import ProblemTagService, get_problem_tag_service

__all__ = ["ProblemTagger", "ProblemTagService", "get_problem_tag_service"]
