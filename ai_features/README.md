# AI Features for LQDOJ

Business logic layer for all AI-powered features in LQDOJ. Uses `llm_service/` for LLM API interactions.

## Features

### Problem Features
- **Problem Tagging**: Predict difficulty (points) and algorithmic types using problem statements and author solutions
- **Markdown Improvement**: Auto-format problem statements with proper sections (####Input, ####Output, etc.)
- **Solution Generation**: Generate editorials from problem statements + accepted code

### Quiz Features
- **Question Markdown Improvement**: Improve formatting of quiz question content and choices
- **Explanation Generation**: Generate or improve explanations for quiz questions
- **Document Import**: Extract quiz questions from PDF/image/Word documents using AI

## Dependencies

This package depends on the general LLM service (`llm_service/`) for API interactions:
- `llm_service.llm_api.LLMService` - General LLM API wrapper
- `llm_service.config.get_config` - Configuration management

## Configuration

Configure in Django `local_settings.py`:
```python
POE_API_KEY = "your-poe-api-key"
POE_BOT_NAME = "Claude-Sonnet-4.6"     # Default model
POE_BOT_NAME_TAGGING = "Gemini-3-Flash" # Optional: model for tagging
POE_BOT_NAME_MARKDOWN = ""              # Optional: model for markdown improvement
POE_BOT_NAME_SOLUTION = ""              # Optional: model for solution generation
```

## Usage

### Problem Tagging Command
```bash
# Tag specific problems
python manage.py tag_problems --codes "prob1,prob2" --update-db

# Tag all public problems (dry run)
python manage.py tag_problems --all-problems --public-only --dry-run
```

### Python API
```python
from ai_features import get_problem_tag_service

tag_service = get_problem_tag_service()

# Tag a problem
result = tag_service.tag_single_problem(problem)

# Improve markdown
result = tag_service.improve_problem_markdown(problem)

# Generate solution
result = tag_service.generate_problem_solution(problem)
```

## File Structure

```
ai_features/
├── problem_tagger.py       # Problem difficulty & tag prediction
├── markdown_improver.py    # Problem statement formatting
├── solution_generator.py   # Editorial generation
├── problem_tag_service.py  # Django integration for problem features
├── quiz_ai_service.py      # Quiz question formatting & explanation
├── quiz_import_service.py  # Document analysis for quiz import
├── CLAUDE.md               # Claude Code instructions
└── README.md               # This file
```

## Celery Tasks

All AI features run as background tasks. Celery must be running:
```bash
celery -A dmoj_celery worker
```

Task definitions: `judge/tasks/llm.py`, `judge/tasks/quiz_import.py`
