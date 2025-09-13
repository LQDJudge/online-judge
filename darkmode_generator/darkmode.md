# Dark Mode CSS Generator

A simple tool to automatically generate dark mode CSS for the LQDOJ platform using DarkReader.

## Quick Start

```bash
cd darkmode_generator
npm install
node generate_darkmode.js
```

## What It Does

This script:
1. Launches a headless browser
2. Loads your LQDOJ site
3. Injects DarkReader
4. Generates dark mode CSS
5. Saves two files in `resources/`:
   - `darkmode.css` - Full CSS with formatting
   - `darkmode.min.css` - Minified version

## Prerequisites

- Node.js installed
- Django server running (default: http://localhost:8000)

## Installation

```bash
cd darkmode_generator
npm install
```

## Usage

Generate dark mode CSS for default URL (http://localhost:8000):
```bash
node generate_darkmode.js
```

Generate for a custom URL:
```bash
node generate_darkmode.js http://localhost:3000
```

## How It Works

The script uses Puppeteer to:
1. Open your site in a headless Chrome browser
2. Inject DarkReader library from CDN
3. Enable DarkReader with default settings
4. Export the generated CSS
5. Save both regular and minified versions

## Manual Method (Browser Console)

If you prefer to generate CSS manually:

1. Add to your HTML temporarily:
```html
<script src="https://cdn.jsdelivr.net/npm/darkreader@4.9.109/darkreader.min.js"></script>
```

2. Open browser console (F12) and run:
```javascript
// Enable DarkReader
DarkReader.enable();

// Export CSS (after a few seconds)
const css = await DarkReader.exportGeneratedCSS();
console.log(css);
```

3. Copy the output and save to a file

## Integration

To use the generated CSS in your Django templates:

```django
<!-- Auto dark mode based on system preference -->
<link rel="stylesheet" href="{% static 'darkmode.min.css' %}" 
      media="(prefers-color-scheme: dark)">

<!-- Or controlled by user preference -->
{% if user.profile.dark_mode %}
  <link rel="stylesheet" href="{% static 'darkmode.min.css' %}">
{% endif %}
```

## Output

The script generates:
- `resources/darkmode.css` - ~500KB formatted CSS
- `resources/darkmode.min.css` - ~300KB minified CSS

Both files include all necessary styles to convert the entire site to dark mode.

## Troubleshooting

**Error: puppeteer is not installed**
```bash
npm install puppeteer
```

**Error: Cannot connect to server**
Make sure Django is running:
```bash
python3 manage.py runserver
```

**Browser download issues**
Puppeteer will download Chromium on first run (~170MB). Ensure you have a stable internet connection.

## Notes

- The generated CSS is large but comprehensive
- Covers all elements automatically
- No manual color adjustments needed
- Works with dynamic content
- Preserves images and icons