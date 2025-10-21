# GUI Branding Configuration Guide

This guide explains how to customize the appearance of the Ollama Proxy Server GUI through environment variables in your `.env` file.

## Overview

The Ollama Proxy Server supports several branding customizations that can be configured through the `.env` file during startup. These settings allow you to personalize the GUI to match your organization's branding.

## Available Branding Options

### 1. BRANDING_TITLE
**Description:** The title displayed in the sidebar header and footer.  
**Type:** String  
**Default:** `Ollama Proxy`  
**Example:**
```
BRANDING_TITLE=My Company AI Proxy
```

### 2. BRANDING_LOGO_URL
**Description:** URL to a logo image displayed in the sidebar header.  
**Type:** String (URL)  
**Default:** Empty (no logo)  
**Example:**
```
BRANDING_LOGO_URL=https://example.com/logo.png
```

### 3. BRANDING_SHOW_LOGO
**Description:** Whether to display the logo in the sidebar header.  
**Type:** Boolean (`true` or `false`)  
**Default:** `false`  
**Example:**
```
BRANDING_SHOW_LOGO=true
```

### 4. BRANDING_SIDEBAR_BG_COLOR (NEW!)
**Description:** Tailwind CSS background color class for the sidebar.  
**Type:** Tailwind CSS class  
**Default:** `bg-gray-800`  
**Supported Values:** Any valid Tailwind dark color class  

## Sidebar Color Options

The sidebar background color is applied using Tailwind CSS utility classes. For best visual results, use dark background colors to ensure good contrast with the white text.

### Popular Color Options:

| Class | Color | Use Case |
|-------|-------|----------|
| `bg-gray-800` | Dark Gray | Default, neutral, professional |
| `bg-gray-900` | Darker Gray | High contrast, very dark |
| `bg-blue-900` | Dark Blue | Tech-focused, calm, professional |
| `bg-indigo-900` | Dark Indigo | Modern, sleek appearance |
| `bg-purple-900` | Dark Purple | Creative, modern feel |
| `bg-slate-800` | Dark Slate | Contemporary, sophisticated |
| `bg-zinc-800` | Dark Zinc | Minimalist, clean |
| `bg-stone-800` | Dark Stone | Warm, earthy tone |
| `bg-red-900` | Dark Red | Bold, attention-grabbing |
| `bg-green-900` | Dark Green | Natural, eco-friendly |
| `bg-amber-900` | Dark Amber | Warm, inviting |

## Configuration Examples

### Example 1: Blue Sidebar with Logo
```bash
BRANDING_TITLE=Acme Corp AI
BRANDING_LOGO_URL=https://acmecorp.com/logo.png
BRANDING_SHOW_LOGO=true
BRANDING_SIDEBAR_BG_COLOR=bg-blue-900
```

### Example 2: Purple Sidebar (Modern Look)
```bash
BRANDING_TITLE=TechStart AI Hub
BRANDING_SIDEBAR_BG_COLOR=bg-purple-900
```

### Example 3: Green Sidebar (Eco-Friendly)
```bash
BRANDING_TITLE=Green Energy AI
BRANDING_SIDEBAR_BG_COLOR=bg-green-900
```

## How to Set Up

1. **Create or edit your `.env` file** in the project root directory:
   ```bash
   cp .env.example .env
   ```

2. **Customize the branding options:**
   ```
   BRANDING_TITLE=Your Company Name
   BRANDING_SIDEBAR_BG_COLOR=bg-blue-900
   ```

3. **Restart the server** for changes to take effect:
   ```bash
   ./run.sh  # On Linux/Mac
   run_windows.bat  # On Windows
   ```

## Docker Deployment

When using Docker, pass the `.env` file as shown in the README:

```bash
docker run -d --name ollama-proxy \
  -p 8080:8080 \
  --env-file ./.env \
  -v ./ollama_proxy.db:/home/app/ollama_proxy.db \
  ollama-proxy-server
```

## Design Considerations

- **Contrast:** All sidebar colors are dark to maintain good contrast with white text
- **Consistency:** The sidebar open button (mobile) uses the same color as the sidebar
- **Responsive:** Branding applies consistently across all screen sizes
- **Performance:** All colors are standard Tailwind classes, no custom CSS needed

## Troubleshooting

**The sidebar color isn't changing:**
- Ensure the `.env` file exists in the project root
- Verify the Tailwind CSS class name is spelled correctly (e.g., `bg-blue-900`, not `bg-blue`)
- Restart the server after making changes
- Clear your browser cache (Ctrl+Shift+Delete or Cmd+Shift+Delete)

**The color doesn't look right:**
- Ensure you're using a dark background color (not light colors like `bg-yellow-100`)
- The sidebar text is white, so light colors will be unreadable
- Test with one of the recommended colors from the table above

## Custom Colors

If you need a color not in the list above, any valid Tailwind CSS dark background class should work. For a complete list of available Tailwind colors, visit: https://tailwindcss.com/docs/background-color
