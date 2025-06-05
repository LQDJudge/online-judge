# Problem Tagging for LQDOJ

This package provides AI-powered problem analysis for LQDOJ using large language models to predict difficulty ratings and algorithmic tags.

## Features

- **Problem Analysis**: Predict difficulty (points) and algorithmic types using problem statements and author solutions
- **PDF Support**: Automatically detects and analyzes PDF statements from `problem.pdf_description`
- **Multi-Problem Support**: Handles PDFs/images containing multiple problems by using problem name/code for identification
- **Format Validation**: Only updates database for problems with valid, complete formats
- **Django Integration**: Works seamlessly with LQDOJ Problem models
- **Management Command**: Batch process problems via Django command

## Dependencies

This package depends on the general LLM service (`llm_service/`) for API interactions:
- `llm_service.llm_api.LLMService` - General LLM API wrapper
- `llm_service.config.get_config` - Configuration management

## Installation & Configuration

1. Install dependencies:
```bash
pip install -r llm_service/requirements.txt
```

2. Configure API key and settings in Django `local_settings.py`:
```python
# Poe API Configuration
POE_API_KEY = "your-poe-api-key"
POE_BOT_NAME = "Claude-3.7-Sonnet"  # Optional, default: Claude-3.7-Sonnet
POE_SLEEP_TIME = 2.5                # Optional, default: 2.5 seconds
POE_TIMEOUT = 30                    # Optional, default: 30 seconds
POE_MAX_RETRIES = 5                 # Optional, default: 5

# Storage settings for file support
MEDIA_ROOT = '/path/to/your/media/files'              # For images, user uploads
DMOJ_PROBLEM_DATA_ROOT = '/path/to/your/problem/data' # For problem PDFs
```

## Usage

### Django Management Command

The `tag_problems` command provides comprehensive options for problem analysis:

#### Basic Usage
```bash
# Tag first 10 problems
python manage.py tag_problems

# Tag specific problems
python manage.py tag_problems --codes "problem1,problem2,problem3"

# Tag a single problem
python manage.py tag_problems --codes "single_problem" --update-db
```

#### Advanced Options
```bash
# Tag all problems
python manage.py tag_problems --all-problems --update-db

# Only tag public problems
python manage.py tag_problems --public-only --limit 50 --update-db

# Update only difficulty points, not types
python manage.py tag_problems --codes "prob1,prob2" --update-db --update-points

# Update only types, not difficulty
python manage.py tag_problems --codes "prob1,prob2" --update-db --update-types

# Dry run to see what would be processed
python manage.py tag_problems --all-problems --public-only --dry-run

# Save results to file without updating database
python manage.py tag_problems --limit 20 --output-file results.txt
```

### Python API

```python
from problem_tag import get_problem_tag_service
from judge.models import Problem

# Initialize service
tag_service = get_problem_tag_service()

# Tag single problem
problem = Problem.objects.get(code='example')
result = tag_service.tag_single_problem(problem)

# Tag batch of problems
codes = ['prob1', 'prob2', 'prob3']
results = tag_service.tag_problem_batch(codes)

# Update problem with results (only if format is valid)
if result['success'] and result['is_valid']:
    tag_service.update_problem_with_tags(problem, result)

# Get problems by codes
problems = tag_service.get_problems_by_codes(['prob1', 'prob2', 'prob3'])
```

## File Structure

```
problem_tag/
├── __init__.py               # Package exports
├── problem_tagger.py         # Core problem analysis with format validation
├── problem_tag_service.py    # Django Problem model integration
└── README.md                 # This file
```

## What are "types"?

In LQDOJ, "types" refer to algorithmic categories that help classify problems:
- Examples: `dp`, `graph`, `greedy`, `binary-search`, `number-theory`, etc.
- Purpose: Help users find problems by algorithm type and prepare for contests
- Current status: ~3,100 problems have types, ~1,850 need classification

## PDF Support

The system automatically detects and processes PDF problem statements:

### Supported Patterns
- **Text + PDF**: Problems with both description and PDF reference
- **PDF Only**: Problems with only PDF statements  
- **Multi-Problem PDFs**: Contest problem sets with multiple problems per file

### Storage Systems
- **PDF Files**: Stored in `DMOJ_PROBLEM_DATA_ROOT/{problem_code}/{filename}`
- **Access**: Local files first, then public URLs via `https://lqdoj.edu.vn`

### Multi-Problem Identification
When PDFs contain multiple problems (like contest problem sets), the system automatically includes the problem code and name in the analysis prompt:

```
PROBLEM TO ANALYZE:
- Problem Code: problemB
- Problem Name: Dynamic Programming

If the file contains multiple problems, focus ONLY on the problem that matches the code and name above.
```

## JSON Response Format

The LLM returns a unified JSON response that handles both format validation and prediction:

```json
{
  "is_valid": true,           // Whether problem has complete format
  "points": 1500,             // Difficulty rating (Codeforces-style)
  "tags": ["dp", "graph"]     // 1-4 core algorithmic tags
}
```

- **Format validation**: Checks for complete problem statement, input/output format, constraints, examples
- **Conditional updates**: Database only updated if `is_valid` is `true`
- **Smart retries**: Retries up to 5 times for invalid responses
- **Author solution integration**: Uses accepted author solutions when available for better analysis

## Error Handling

- **Format validation**: Only updates problems with complete, valid formats
- **Automatic retries**: Up to 5 retries for failed API calls or invalid responses
- **File upload fallbacks**: If local PDF files not found, tries public URLs
- **Graceful fallbacks**: Handles unsupported formats and missing author solutions
- **Comprehensive logging**: Full debug information for troubleshooting
- **Transaction safety**: Database updates are atomic and reversible

## Rate Limiting & Performance

- **Configurable delays**: 2.5 second default between API calls
- **Timeout protection**: 30 second timeout for long responses  
- **Batch processing**: Efficient handling of large problem sets with progress tracking
- **Memory efficient**: Processes problems one at a time to avoid memory issues

## Integration with LLM Service

This package uses the general `llm_service` for all LLM interactions:
- File upload and processing
- Multi-format support (images, PDFs, text)
- Dual storage system handling
- Public URL fallback for PDFs