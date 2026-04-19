import os
import subprocess
from pathlib import Path
from typing import Dict, Any

TOOL_LIBRARY_NAME = 'LoLLMs Bot Tools'
TOOL_LIBRARY_DESC = 'Core filesystem and execution primitives for the LoLLMs Bot Agent.'
TOOL_LIBRARY_ICON = '🤖'

TOOL_TITLES = {
    "tool_read_file": "📂 Reading Local File",
    "tool_write_file": "✍️ Writing to Workspace",
    "tool_send_artifact_to_user": "🎁 Preparing Artifact for User",
    "tool_execute_command": "💻 System Terminal"
}

WORKSPACE_DIR = Path("app/static/uploads/workspace")

def init_tool_library() -> None:
    '''Ensure basic system permissions are understood.'''
    # We create a publicly accessible workspace folder if it doesn't exist
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

def tool_read_file(args: dict, lollms=None):
    '''
    Reads the content of a file from the local workspace.
    
    Args:
        args: dict with keys:
            - path (str): Path to the file relative to the workspace.
    '''
    try:
        path = WORKSPACE_DIR / args.get('path', '').lstrip('/\\')
        if not path.exists():
            return f"Error: File '{args.get('path')}' not found."
        
        content = path.read_text(encoding='utf-8', errors='ignore')
        return f"--- FILE CONTENT: {args.get('path')} ---\n{content}"
    except Exception as e:
        return f"Error reading file: {str(e)}"

def tool_write_file(args: dict, lollms=None):
    '''
    Creates or overwrites a file in the local workspace.
    
    Args:
        args: dict with keys:
            - path (str): Destination path relative to workspace.
            - content (str): The text content to write.
    '''
    try:
        path = WORKSPACE_DIR / args.get('path', '').lstrip('/\\')
        path.parent.mkdir(parents=True, exist_ok=True)
        
        path.write_text(args.get('content', ''), encoding='utf-8')
        return f"Success: Wrote {len(args.get('content', ''))} characters to '{args.get('path')}'."
    except Exception as e:
        return f"Error writing file: {str(e)}"

def tool_send_artifact_to_user(args: dict, lollms=None):
    '''
    Send a file from the workspace to the user. Use this when the user asks to see a generated file, image, or document.
    
    Args:
        args: dict with keys:
            - path (str): Path to the file relative to the workspace.
    '''
    try:
        path = WORKSPACE_DIR / args.get('path', '').lstrip('/\\')
        if not path.exists():
            return f"Error: File '{args.get('path')}' not found."
            
        public_path = f"/static/uploads/workspace/{args.get('path', '').lstrip('/\\\\')}"
        return f"File sent successfully.\n<artifact type=\"file\" path=\"{public_path}\"/>"
    except Exception as e:
        return f"Error sending file: {str(e)}"

def tool_execute_command(args: dict, lollms=None):
    '''
    Executes a shell command or script and returns the console output.
    
    Args:
        args: dict with keys:
            - command (str): The shell command to run (e.g. 'python script.py' or 'ls -la').
    '''
    try:
        # Execute within the workspace directory
        result = subprocess.run(
            args.get('command'),
            shell=True,
            capture_output=True,
            text=True,
            cwd=str(WORKSPACE_DIR.absolute()),
            timeout=30 # Safety timeout
        )
        
        output = result.stdout
        if result.stderr:
            output += f"\n--- ERROR OUTPUT ---\n{result.stderr}"
            
        return f"Command executed (Exit Code: {result.returncode})\nOutput:\n{output if output else '[No Output]'}"
    except subprocess.TimeoutExpired:
        return "Error: Command timed out after 30 seconds."
    except Exception as e:
        return f"Execution Error: {str(e)}"