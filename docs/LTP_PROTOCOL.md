# 📜 LTP: LoLLMs Tools Protocol

The **LoLLMs Tools Protocol (LTP)** is a metadata standard for Python-based tool libraries. It allows tools to be "self-describing," enabling the LoLLMs Hub Gateway and various Frontends (Discord, Web, Telegram) to display human-friendly telemetry instead of raw function calls.

---

## 1. Core Metadata Variables

Every LTP-compliant tool library should define these global variables at the top of the `.py` file:

| Variable | Type | Description |
| :--- | :--- | :--- |
| `TOOL_LIBRARY_NAME` | `str` | The title of the entire library (e.g., "Financial Analyst"). |
| `TOOL_LIBRARY_DESC` | `str` | A brief explanation of the library's capabilities. |
| `TOOL_LIBRARY_ICON` | `str` | An emoji or icon representing the toolset. |
| `TOOL_TITLES` | `dict` | **The Heart of LTP.** Maps function names to UI display strings. |

---

## 2. The `TOOL_TITLES` Schema

The `TOOL_TITLES` dictionary provides a human-readable "Status Message" for every function in the library. When the Agent calls a function, the Hub recovers this title for the live stream.

### Example Implementation:
```python
TOOL_TITLES = {
    "tool_get_weather": "🌤️ Checking local weather",
    "tool_search_wikipedia": "📖 Consulting Wikipedia",
    "tool_write_file": "✍️ Saving data to disk"
}

def tool_get_weather(args):
    # logic...
```

---

## 3. Function Naming Standard

To be discovered by the LTP parser, all callable functions MUST follow the prefix rule:
- **Correct**: `def tool_my_logic(args):`
- **Incorrect**: `def my_logic(args):` (Treated as a private helper, hidden from AI)

---

## 4. Argument Standards

For consistent "Pretty Text" previews in the UI, LTP recommends using standard key names in the `args` dictionary:
- `query`: For search terms.
- `path`: For file operations.
- `url`: For web scraping.
- `command`: For terminal execution.

The Hub automatically extracts these keys to create context previews:
> **📖 Consulting Wikipedia**: "Quantum Computing"

---

## 5. Benefits of LTP compliance
1. **Zero-Code UI**: Your tools automatically look professional in the LoLLMs Playground.
2. **Multi-User Logging**: Administrators can see exactly what tools are being used in a human-readable format.
3. **Bot Compatibility**: Telegram and Discord bots use LTP titles to show "Thinking..." status messages to users.