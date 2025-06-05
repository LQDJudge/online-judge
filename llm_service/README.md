# LLM Service for LQDOJ

This package provides general LLM API functionality for LQDOJ using large language models via Poe API.

## Features

- **General LLM API**: Clean, reusable interface for any LLM task
- **File Upload Support**: Automatic file extraction, upload to Poe, and analysis from markdown content
- **Multi-format Support**: Images, PDFs, and text files via `fp.upload_file_sync`
- **Local & Remote Files**: Supports both public URLs and local files (via storage systems)
- **Dual Storage Support**: Handles both MEDIA_ROOT files and DMOJ_PROBLEM_DATA_ROOT files (PDFs)
- **Configuration Management**: Centralized configuration with Django settings integration

## Installation

1. Install dependencies:
```bash
pip install -r llm_service/requirements.txt
```

2. Configure API key and storage settings in Django `local_settings.py`:
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

### Direct LLM API

```python
from llm_service.llm_api import LLMService

llm_service = LLMService(api_key="your-api-key", bot_name="Claude-3.7-Sonnet")

# Simple prompt-response
response = llm_service.call_llm("What is 2+2?")

# With system prompt
response = llm_service.call_llm(
    prompt="Solve this problem",
    system_prompt="You are a math tutor"
)

# With files (automatic extraction and upload from markdown)
markdown_with_files = """
![public diagram](https://example.com/image.png)
![local image](/media/pagedown-uploads/local_image.png)
![also works](/pagedown-uploads/local_image.png)
[Problem PDF](https://example.com/problem.pdf)
[Local PDF](/media/uploads/problem_statement.pdf)
Analyze this problem...
"""
response = llm_service.call_llm_with_files(
    prompt="Analyze this problem:",
    content_with_files=markdown_with_files,
    system_prompt="You are a competitive programming judge"
)
```

## File Structure

```
llm_service/
├── __init__.py
├── llm_api.py              # General LLM API wrapper with file support
├── config.py              # Configuration management
├── requirements.txt       # Python dependencies
└── README.md              # This file
```

## Configuration

The service uses configuration from Django settings first, then falls back to environment variables:

**Django settings** (recommended):
```python
POE_API_KEY = "your-api-key"
POE_BOT_NAME = "Claude-3.7-Sonnet"
POE_SLEEP_TIME = 2.5
```

**Environment variables** (fallback):
```bash
export POE_API_KEY="your-api-key"
export POE_BOT_NAME="Claude-3.7-Sonnet"
export POE_SLEEP_TIME="2.5"
```

## Supported Features

- **File Upload & Analysis**: Automatic extraction, upload to Poe, and analysis of files from markdown
- **Multi-format Support**: Images (PNG, JPG, GIF, WebP), PDFs, and text files
- **Public File URLs**: Supports publicly accessible file URLs (http/https)
- **Dual Storage Support**: Handles both MEDIA_ROOT files and DMOJ_PROBLEM_DATA_ROOT files (PDFs)
- **Local File Support**: Supports local files via Django MEDIA_ROOT and DMOJ_PROBLEM_DATA_ROOT settings
- **Fallback Download**: Automatic download and upload if direct URL upload fails
- **File Size Limits**: Automatic 50MB size limit for local files
- **Flexible URL Patterns**: Supports both `/media/path/file.ext` and `/path/file.ext` for local files
- **Public URL Fallback**: For PDFs, automatically tries public URLs if local files not found

## File Storage Systems

The LLM service supports two different storage systems used by LQDOJ:

### 1. Regular Media Files (MEDIA_ROOT)
- **Location**: Django's `MEDIA_ROOT` setting (e.g., `/home/user/LQDOJ/media`)
- **File Types**: Images, user uploads, pagedown uploads
- **URL Patterns**: 
  - `/media/path/file.ext`
  - `/path/file.ext` (without media prefix)

### 2. Problem Data Files (DMOJ_PROBLEM_DATA_ROOT)
- **Location**: LQDOJ's `DMOJ_PROBLEM_DATA_ROOT` setting (e.g., `/home/user/LQDOJ/problems`)
- **File Types**: Problem PDFs, test data, custom checkers
- **URL Pattern**: `/problem/{problem_code}/data/{filename}`
- **Storage Path**: `{DMOJ_PROBLEM_DATA_ROOT}/{problem_code}/{filename}`

### Automatic Detection & Fallback Strategy

**For PDFs** (`/problem/{code}/data/{filename}`):
1. **Try Local First**: Check `{DMOJ_PROBLEM_DATA_ROOT}/{code}/{filename}`
2. **Public URL Fallback**: If local file not found, try `https://lqdoj.edu.vn/problem/{code}/data/{filename}`

**For Media Files** (`/media/path/file` or `/path/file`):
1. **Local Only**: Check `{MEDIA_ROOT}/{cleaned_path}` (removes `media/` prefix if present)

**For Public URLs** (`https://lqdoj.edu.vn/...`):
1. **Direct Upload**: Upload directly from the provided URL

## Error Handling

- **Automatic retries**: Up to 5 retries for failed API calls
- **File upload fallbacks**: If direct URL upload fails, tries download + upload approach
- **Graceful fallbacks**: Handles unsupported formats and missing files
- **Comprehensive logging**: Full debug information for troubleshooting
- **File size validation**: Automatic limits to prevent oversized uploads

## Rate Limiting & Performance

- **Configurable delays**: 2.5 second default between API calls
- **Timeout protection**: 30 second timeout for long responses  
- **Memory efficient**: Handles large files without loading everything into memory