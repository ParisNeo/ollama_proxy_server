# Auto-Routing System: Complete Documentation & Visualization

## Table of Contents
1. [Overview](#overview)
2. [System Architecture](#system-architecture)
3. [Request Analysis Matrix](#request-analysis-matrix)
4. [Priority Modes](#priority-modes)
5. [Scoring Algorithm](#scoring-algorithm)
6. [Complete Routing Flow](#complete-routing-flow)
7. [Examples & Scenarios](#examples--scenarios)

---

## Overview

The auto-routing system is an intelligent model selection engine that analyzes incoming requests and automatically selects the best model based on:

- **Request Characteristics**: What the user is asking for (images, code, tool calling, internet access, reasoning, speed)
- **Model Capabilities**: What each model can do (stored in metadata)
- **Priority Modes**: Cost/budget preferences (Free, Daily Drive, Advanced, Luxury)
- **Model Descriptions**: Semantic matching between request and model descriptions
- **Priority Levels**: Pre-assigned priority (1 = highest, 10 = lowest)

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    INCOMING REQUEST                             │
│              (POST /api/chat/completions)                        │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│              STEP 1: REQUEST ANALYSIS                            │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  analyze_request(body)                                   │   │
│  │  • Extract prompt content                                 │   │
│  │  • Detect images in request                               │   │
│  │  • Detect code patterns                                   │   │
│  │  • Check for tool calling requirements                    │   │
│  │  • Check for internet/grounding needs                     │   │
│  │  • Check for thinking/reasoning needs                     │   │
│  │  • Check for fast model preference                       │   │
│  │  • Extract keywords for semantic matching                │   │
│  └──────────────────────────────────────────────────────────┘   │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│              STEP 2: GET AVAILABLE MODELS                        │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  • Fetch all ModelMetadata from database                  │   │
│  │  • Filter to only models on active servers                │   │
│  │  • Build model_details_map (pricing, etc.)                │   │
│  └──────────────────────────────────────────────────────────┘   │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│              STEP 3: APPLY PRIORITY MODE                         │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Priority Mode determines initial priority assignment:    │   │
│  │                                                           │   │
│  │  FREE MODE:        Daily Drive Mode:    Advanced Mode:   │   │
│  │  P1: Free models   P1: Cloud models    P1: Top-tier      │   │
│  │  P2: Cloud models  P2: Free models     P2: Mid-tier      │   │
│  │  P3: Paid models   P3: Paid models     P3: Other paid    │   │
│  │                                                           │   │
│  │  LUXURY MODE:                                            │   │
│  │  P1: Premium ($5+/1M)                                    │   │
│  │  P2: Mid-tier premium ($1-5/1M)                          │   │
│  │  P3: Other paid                                          │   │
│  └──────────────────────────────────────────────────────────┘   │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│              STEP 4: PRIORITY-BASED ITERATION                   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  For each priority level (1, 2, 3, ...):                 │   │
│  │    1. Get all models at this priority                    │   │
│  │    2. Score each model                                   │   │
│  │    3. Filter out models with score < 0                  │   │
│  │    4. If any model has score > 0:                        │   │
│  │       → Return best model from this priority             │   │
│  │    5. Else: Continue to next priority level              │   │
│  └──────────────────────────────────────────────────────────┘   │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│              STEP 5: MODEL SCORING                               │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  score_model(metadata, request_analysis, model_details)  │   │
│  │                                                           │   │
│  │  Base Score (0-60 points):                                │   │
│  │    • Priority 1: 50 points                               │   │
│  │    • Priority 2: 40 points                               │   │
│  │    • Priority 3: 30 points                               │   │
│  │    • Priority 4: 20 points                                │   │
│  │    • ... (decreases by 10 per level)                     │   │
│  │                                                           │   │
│  │  Capability Matching:                                     │   │
│  │    • Required capability present: +10 points              │   │
│  │    • Required capability missing: -30 to -50 points      │   │
│  │                                                           │   │
│  │  Semantic Matching:                                       │   │
│  │    • Keyword matches in description: up to +15 points    │   │
│  │                                                           │   │
│  │  Versatility Bonus:                                       │   │
│  │    • 3+ capabilities: +5 points                          │   │
│  │                                                           │   │
│  │  Luxury Mode Bonus:                                       │   │
│  │    • Premium pricing ($5+/1M): +10 points                │   │
│  │    • Mid-tier pricing ($1-5/1M): +5 points                │   │
│  └──────────────────────────────────────────────────────────┘   │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│              STEP 6: SELECT BEST MODEL                           │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  • Sort models by score (descending)                     │   │
│  │  • Tiebreaker: Lower priority number                     │   │
│  │  • Return model with highest score                       │   │
│  └──────────────────────────────────────────────────────────┘   │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│                    SELECTED MODEL                               │
│              (Returned to proxy for routing)                     │
└─────────────────────────────────────────────────────────────────┘
```

---

## Request Analysis Matrix

The `analyze_request()` function examines the incoming request and determines what capabilities are needed:

### Detection Methods

| Capability | Detection Method | Examples |
|------------|------------------|----------|
| **Images** | • `images` field in body<br>• `content` array with `type: "image"` | `{"images": ["base64..."]}`<br>`{"content": [{"type": "image", ...}]}` |
| **Code** | • Keyword patterns in prompt<br>• Code syntax detection | `"def function():"`, `"class MyClass"`, `"import numpy"`, `"const x = 5"` |
| **Tool Calling** | • `tools` field in body<br>• `tool_choice` field<br>• `tool_calls` in messages | `{"tools": [...]}`, `{"tool_choice": "auto"}` |
| **Internet** | • Keywords in messages<br>• Context analysis | `"web_search"`, `"internet"`, `"grounding"`, `"real-time"`, `"current news"` |
| **Thinking** | • `options.think` field<br>• Keywords in prompt | `{"options": {"think": true}}`, `"think step by step"`, `"chain of thought"` |
| **Fast** | • `options.fast_model` field | `{"options": {"fast_model": true}}` |

### Request Type Classification

```
Request Type Determination:
├─ Has Images?
│  ├─ Yes → Contains Code?
│  │  ├─ Yes → "multimodal_code"
│  │  └─ No  → "multimodal"
│  └─ No → Continue
├─ Contains Code?
│  └─ Yes → "code"
├─ Requires Thinking?
│  └─ Yes → "reasoning"
├─ Requires Tool Calling?
│  └─ Yes → "tool_use"
├─ Requires Internet?
│  └─ Yes → "web_search"
└─ Default → "general"
```

### Keyword Extraction

- Extracts meaningful words (3+ characters) from prompt
- Removes stop words (the, a, an, and, or, but, etc.)
- Limits to top 20 keywords
- Used for semantic matching against model descriptions

---

## Priority Modes

### Free Mode
**Goal**: Minimize costs by prioritizing free models

```
Priority Assignment:
├─ Priority 1: Free models (pricing = $0)
├─ Priority 2: Ollama cloud models (*:cloud)
└─ Priority 3: Paid models (all others)
```

**Use Case**: Budget-conscious users, testing, development

---

### Daily Drive Mode
**Goal**: Balance cost and quality, prefer Ollama cloud

```
Priority Assignment:
├─ Priority 1: Ollama cloud models (*:cloud)
├─ Priority 2: Free models (pricing = $0)
└─ Priority 3: Paid models (all others)
```

**Use Case**: Regular daily use, good balance of cost/quality

---

### Advanced Mode
**Goal**: Prioritize top-tier models, skip free models

```
Priority Assignment:
├─ Priority 1: Top-tier models
│  ├─ Claude 4.5, Claude 4
│  ├─ GPT-5, GPT-5.1
│  ├─ Gemini 3, Gemini 3 Pro
│  └─ O4, O4-mini, O4-mini-high
├─ Priority 2: Mid-tier models
│  ├─ Claude Opus, Claude Sonnet
│  ├─ GPT-4, GPT-4.1
│  └─ Gemini 2.5 Pro, Gemini 2.5 Flash
└─ Priority 3: Other paid models
```

**Use Case**: Professional work, high-quality outputs needed

---

### Luxury Mode
**Goal**: Premium models for high-budget scenarios

```
Priority Assignment:
├─ Priority 1: Premium models ($5+ per 1M input tokens)
│  └─ Top-tier models with high pricing
├─ Priority 2: Mid-tier premium ($1-5 per 1M input tokens)
│  └─ Opus-level models
└─ Priority 3: Other paid models (<$1 per 1M tokens)
```

**Use Case**: High-budget projects, maximum quality required

---

## Scoring Algorithm

### Score Calculation Formula

```
Total Score = Base Score + Capability Bonuses - Capability Penalties + Semantic Match + Versatility Bonus + Budget Bonus
```

### Detailed Scoring Breakdown

#### 1. Base Score (0-60 points)
Based on priority level:
- Priority 1: **50 points**
- Priority 2: **40 points**
- Priority 3: **30 points**
- Priority 4: **20 points**
- Priority 5: **10 points**
- Priority 6+: **0 points**
- No priority: **20 points** (default)

#### 2. Capability Matching

**Bonuses** (when capability is present and needed):
- Images: **+10 points**
- Code: **+10 points**
- Tool Calling: **+10 points**
- Internet: **+10 points**
- Thinking: **+10 points**
- Fast: **+5 points**

**Penalties** (when capability is required but missing):
- Images: **-50 points** (critical)
- Tool Calling: **-50 points** (critical)
- Internet: **-50 points** (critical)
- Code: **-30 points** (important)
- Thinking: **-30 points** (important)
- Fast: **-20 points** (moderate)

**Note**: Models with negative total scores are **excluded** from selection.

#### 3. Semantic Matching (0-15 points)

- Extracts keywords from request prompt
- Matches keywords against model description
- Formula: `min(15, (matching_keywords / total_keywords) * 15)`
- Example: 5 matching keywords out of 10 = **7.5 points**

#### 4. Versatility Bonus (+5 points)

- Awarded if model has **3+ capabilities** enabled
- Encourages selection of versatile models when no specific requirement dominates

#### 5. Budget Bonus (Luxury Mode Only)

- Premium pricing ($5+/1M tokens): **+10 points**
- Mid-tier pricing ($1-5/1M tokens): **+5 points**

### Score Range

- **Maximum**: 100.0 points
- **Minimum**: 0.0 points (models with negative scores are excluded)
- **Typical Range**: 20-80 points

---

## Complete Routing Flow

### Detailed Step-by-Step Process

```
┌─────────────────────────────────────────────────────────────────────┐
│ PHASE 1: REQUEST RECEIVED                                            │
└─────────────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│ PHASE 2: REQUEST ANALYSIS                                            │
│                                                                       │
│  Input: Request body (JSON)                                           │
│  Output: Request analysis dict                                       │
│                                                                       │
│  Steps:                                                               │
│  1. Extract prompt content (from "prompt" or "messages")            │
│  2. Check for images (body["images"] or content array)              │
│  3. Detect code patterns (keywords: def, class, import, etc.)        │
│  4. Check for tool calling (body["tools"], body["tool_choice"])     │
│  5. Check for internet needs (keywords: web_search, internet, etc.) │
│  6. Check for thinking needs (options.think or keywords)            │
│  7. Check for fast model preference (options.fast_model)            │
│  8. Extract keywords for semantic matching                           │
│  9. Classify request type (code, multimodal, reasoning, etc.)       │
└─────────────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│ PHASE 3: MODEL COLLECTION                                            │
│                                                                       │
│  Steps:                                                               │
│  1. Fetch all ModelMetadata from database                            │
│  2. Get list of available model names from active servers           │
│  3. Filter metadata to only available models                         │
│  4. Build model_details_map from server.available_models            │
│     (includes pricing, context length, etc.)                          │
└─────────────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│ PHASE 4: PRIORITY MODE APPLICATION                                   │
│                                                                       │
│  Based on settings.auto_routing_priority_mode:                       │
│                                                                       │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │ FREE MODE                                                    │    │
│  │ • Separate models into: free, cloud, paid                   │    │
│  │ • Assign priorities: P1=free, P2=cloud, P3=paid             │    │
│  └──────────────────────────────────────────────────────────────┘    │
│                                                                       │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │ DAILY DRIVE MODE                                             │    │
│  │ • Separate models into: cloud, free, paid                   │    │
│  │ • Assign priorities: P1=cloud, P2=free, P3=paid             │    │
│  └──────────────────────────────────────────────────────────────┘    │
│                                                                       │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │ ADVANCED MODE                                                │    │
│  │ • Separate models into: top-tier, mid-tier, other paid      │    │
│  │ • Skip free models                                          │    │
│  │ • Assign priorities: P1=top-tier, P2=mid-tier, P3=other    │    │
│  └──────────────────────────────────────────────────────────────┘    │
│                                                                       │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │ LUXURY MODE                                                   │    │
│  │ • Separate by pricing: premium, mid-tier, other             │    │
│  │ • Skip free models                                           │    │
│  │ • Assign priorities: P1=premium, P2=mid-tier, P3=other     │    │
│  └──────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│ PHASE 5: PRIORITY-BASED ITERATION                                    │
│                                                                       │
│  Get sorted list of priority levels (1, 2, 3, ...)                  │
│                                                                       │
│  For each priority level (starting with 1):                          │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │ 1. Get all models with this priority                         │    │
│  │ 2. Score each model:                                         │    │
│  │    score_model(metadata, request_analysis, model_details)   │    │
│  │ 3. Filter: Keep only models with score >= 0                  │    │
│  │ 4. If any models remain:                                     │    │
│  │    • Sort by score (descending)                             │    │
│  │    • Tiebreaker: Lower priority number                      │    │
│  │    • Return best model from this priority level              │    │
│  │ 5. Else: Continue to next priority level                     │    │
│  └──────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│ PHASE 6: FALLBACK HANDLING                                          │
│                                                                       │
│  If no model found at any priority level:                            │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │ 1. Try highest priority level again (ignore score)           │    │
│  │ 2. If still no model: Use first available model             │    │
│  │ 3. Log warning about fallback                                │    │
│  └──────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│ PHASE 7: MODEL SELECTED                                              │
│                                                                       │
│  Return selected model name to proxy for routing                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Examples & Scenarios

### Example 1: Code Generation Request

**Request:**
```json
{
  "model": "auto",
  "messages": [
    {
      "role": "user",
      "content": "Write a Python function to calculate fibonacci numbers"
    }
  ]
}
```

**Analysis:**
- `contains_code`: ✅ True (detected "def", "function", "Python")
- `has_images`: ❌ False
- `requires_tool_calling`: ❌ False
- `requires_internet`: ❌ False
- `requires_thinking`: ❌ False
- `request_type`: "code"

**Routing (Free Mode):**
1. Priority 1 (Free models):
   - `deepseek-coder:free` - Score: 50 (base) + 10 (code) = **60**
   - `codellama:7b` - Score: 50 (base) + 10 (code) = **60**
   - **Selected**: `deepseek-coder:free` (tiebreaker: first in list)

**Result**: Free code model selected ✅

---

### Example 2: Image Analysis Request

**Request:**
```json
{
  "model": "auto",
  "messages": [
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "What's in this image?"},
        {"type": "image", "image": "base64..."}
      ]
    }
  ]
}
```

**Analysis:**
- `has_images`: ✅ True
- `contains_code`: ❌ False
- `requires_tool_calling`: ❌ False
- `requires_internet`: ❌ False
- `request_type`: "multimodal"

**Routing (Daily Drive Mode):**
1. Priority 1 (Cloud models):
   - `gemini-2.5-pro:cloud` - Score: 50 (base) + 10 (images) = **60**
   - `gpt-4o:cloud` - Score: 50 (base) + 10 (images) = **60**
   - **Selected**: `gemini-2.5-pro:cloud`

**Result**: Cloud multimodal model selected ✅

---

### Example 3: Tool Calling Request

**Request:**
```json
{
  "model": "auto",
  "messages": [
    {
      "role": "user",
      "content": "Get the current weather in San Francisco"
    }
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "Get weather for a location"
      }
    }
  ]
}
```

**Analysis:**
- `requires_tool_calling`: ✅ True (tools field present)
- `has_images`: ❌ False
- `contains_code`: ❌ False
- `request_type`: "tool_use"

**Routing (Advanced Mode):**
1. Priority 1 (Top-tier):
   - `claude-4.5-sonnet` - Score: 50 (base) + 10 (tool calling) = **60**
   - `gpt-5` - Score: 50 (base) + 10 (tool calling) = **60**
   - **Selected**: `claude-4.5-sonnet`

**Result**: Top-tier model with tool calling selected ✅

---

### Example 4: Internet Search Request

**Request:**
```json
{
  "model": "auto",
  "messages": [
    {
      "role": "user",
      "content": "What's the latest news about AI developments today? I need real-time information."
    }
  ]
}
```

**Analysis:**
- `requires_internet`: ✅ True (detected "real-time", "today", "latest news")
- `has_images`: ❌ False
- `contains_code`: ❌ False
- `request_type`: "web_search"

**Routing (Free Mode):**
1. Priority 1 (Free models):
   - `deepseek-r1:free` - Score: 50 (base) - 50 (no internet) = **0** ❌
   - `llama-3.1:8b` - Score: 50 (base) - 50 (no internet) = **0** ❌
   - All free models excluded (missing internet capability)

2. Priority 2 (Cloud models):
   - `gemini-3-pro:cloud` - Score: 40 (base) + 10 (internet) = **50** ✅
   - **Selected**: `gemini-3-pro:cloud`

**Result**: Cloud model with internet grounding selected ✅

---

### Example 5: Thinking/Reasoning Request

**Request:**
```json
{
  "model": "auto",
  "messages": [
    {
      "role": "user",
      "content": "Think step by step: If a train leaves Station A at 60 mph and another leaves Station B at 80 mph..."
    }
  ],
  "options": {
    "think": true
  }
}
```

**Analysis:**
- `requires_thinking`: ✅ True (options.think + "step by step")
- `has_images`: ❌ False
- `contains_code`: ❌ False
- `request_type`: "reasoning"

**Routing (Luxury Mode):**
1. Priority 1 (Premium models):
   - `o4-mini` - Score: 50 (base) + 10 (thinking) + 10 (luxury bonus) = **70**
   - `claude-4.5-sonnet` - Score: 50 (base) + 10 (thinking) + 10 (luxury bonus) = **70**
   - **Selected**: `o4-mini` (specialized thinking model)

**Result**: Premium thinking model selected ✅

---

## Model Capability Detection

### Automatic Detection (Heuristics)

When a model is first discovered, the system uses heuristics to set default capabilities:

| Capability | Detection Patterns |
|------------|-------------------|
| **Images** | `llava`, `bakllava`, `vision`, `gpt-4`, `claude-3`, `gemini` |
| **Code** | `code`, `codellama`, `deepseek-coder`, `starcoder` |
| **Fast** | `7b`, `8b`, `3b`, `1b`, `turbo`, `fast` |
| **Tool Calling** | `gpt-4`, `gpt-3.5`, `claude-3`, `claude-4`, `gemini`, `o1`, `tool` |
| **Internet** | `grok`, `perplexity`, `you.com`, `phind`, `brave`, `search`, `grounding`, `web-browsing`, `internet-access` |
| **Thinking** | `thinking`, `think`, `o1`, `o3`, `reasoning`, `cot` |

### Manual Override

Admins can manually adjust capabilities in the Models Manager:
- Use "Smart AI" button to auto-detect and fill
- Manually check/uncheck capability boxes
- Edit description for better semantic matching

---

## Performance Considerations

### Optimization Strategies

1. **Priority-Based Filtering**: Only evaluates models at current priority level before moving to next
2. **Early Exit**: Returns immediately when suitable model found at a priority level
3. **Negative Score Exclusion**: Models missing required capabilities are excluded early
4. **Caching**: Model metadata and details are cached in memory during request processing

### Typical Performance

- **Request Analysis**: < 1ms
- **Model Collection**: 5-20ms (database query)
- **Scoring (per model)**: < 0.1ms
- **Total Routing Time**: 10-50ms (depending on number of models)

---

## Troubleshooting

### No Model Selected

**Possible Causes:**
1. No models have required capabilities
2. All models at priority levels have negative scores
3. No models available on active servers

**Solution**: Check logs for specific reason, adjust model capabilities or priority assignments

### Wrong Model Selected

**Possible Causes:**
1. Model capabilities not correctly set
2. Priority mode doesn't match use case
3. Description doesn't match request keywords

**Solution**: 
- Review model capabilities in Models Manager
- Adjust priority mode in Settings
- Improve model descriptions for better semantic matching

### Models Skipped

**Possible Causes:**
1. Model missing required capability (negative score)
2. Model not on active server
3. Model priority not assigned

**Solution**: 
- Check model capabilities
- Ensure server is active
- Run "Auto-Priority Inventory" to assign priorities

---

## Configuration

### Settings

Located in **Settings → Auto-Routing**:

- **Priority Mode**: `free`, `daily_drive`, `advanced`, or `luxury`
- **Enable Auto-Routing**: Toggle on/off (default: on)

### Model Metadata

Located in **Models Manager**:

- **Priority**: Manual assignment (1-10) or use "Auto-Priority Inventory"
- **Capabilities**: Checkboxes for images, code, tool calling, internet, thinking, fast
- **Description**: AI-generated or manual (used for semantic matching)

---

## Conclusion

The auto-routing system provides intelligent, context-aware model selection that:

✅ **Maximizes efficiency** by selecting models with required capabilities  
✅ **Respects budget** through priority modes  
✅ **Improves quality** through semantic matching and capability detection  
✅ **Handles edge cases** with fallback mechanisms  
✅ **Performs quickly** with optimized priority-based iteration  

For questions or issues, check the logs for detailed routing decisions and scores.

