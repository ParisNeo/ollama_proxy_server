"""
Codebase indexing service for RAG
Indexes Python files and other code files for intelligent codebase awareness
"""
import logging
import os
from pathlib import Path
from typing import List, Dict, Any, Optional
import re

logger = logging.getLogger(__name__)


class CodebaseIndexer:
    """Indexes codebase files for RAG retrieval"""
    
    def __init__(self, root_path: Optional[str] = None):
        self.root_path = Path(root_path) if root_path else Path(__file__).parent.parent.parent
        self.indexed_files: Dict[str, Dict[str, Any]] = {}
        
    def should_index_file(self, file_path: Path) -> bool:
        """Determine if a file should be indexed"""
        # Skip hidden files and directories
        if any(part.startswith('.') for part in file_path.parts):
            return False
        
        # Skip common non-code directories
        skip_dirs = {
            '__pycache__', 'node_modules', '.git', 'venv', 'env', 
            'chroma_db', '.pytest_cache', 'dist', 'build', '.mypy_cache'
        }
        if any(part in skip_dirs for part in file_path.parts):
            return False
        
        # Index Python files, config files, and documentation
        extensions = {'.py', '.md', '.txt', '.yaml', '.yml', '.json', '.toml', '.ini', '.cfg'}
        return file_path.suffix.lower() in extensions
    
    def extract_code_metadata(self, content: str, file_path: Path) -> Dict[str, Any]:
        """Extract metadata from code file"""
        metadata = {
            'file_path': str(file_path.relative_to(self.root_path)),
            'file_name': file_path.name,
            'lines': len(content.splitlines()),
            'functions': [],
            'classes': [],
            'imports': []
        }
        
        if file_path.suffix == '.py':
            # Extract function definitions
            func_pattern = r'^(?:async\s+)?def\s+(\w+)\s*\('
            metadata['functions'] = re.findall(func_pattern, content, re.MULTILINE)
            
            # Extract class definitions
            class_pattern = r'^class\s+(\w+)'
            metadata['classes'] = re.findall(class_pattern, content, re.MULTILINE)
            
            # Extract imports
            import_pattern = r'^(?:from\s+[\w.]+\s+)?import\s+([\w.,\s]+)'
            imports = re.findall(import_pattern, content, re.MULTILINE)
            metadata['imports'] = [imp.strip() for imp in imports if imp.strip()]
        
        return metadata
    
    def chunk_code_file(self, content: str, file_path: Path, chunk_size: int = 1000) -> List[Dict[str, Any]]:
        """Split code file into chunks for better retrieval"""
        chunks = []
        lines = content.splitlines()
        
        # For Python files, try to chunk by functions/classes
        if file_path.suffix == '.py':
            current_chunk = []
            current_function = None
            
            for line in lines:
                # Detect function or class start
                if re.match(r'^(?:async\s+)?def\s+\w+', line) or re.match(r'^class\s+\w+', line):
                    # Save previous chunk
                    if current_chunk:
                        chunks.append({
                            'content': '\n'.join(current_chunk),
                            'type': 'function' if current_function else 'code',
                            'function': current_function
                        })
                    current_chunk = [line]
                    # Extract function/class name
                    match = re.match(r'^(?:async\s+)?def\s+(\w+)', line) or re.match(r'^class\s+(\w+)', line)
                    current_function = match.group(1) if match else None
                else:
                    current_chunk.append(line)
                    
                    # If chunk gets too large, split it
                    if len('\n'.join(current_chunk)) > chunk_size:
                        chunks.append({
                            'content': '\n'.join(current_chunk),
                            'type': 'function' if current_function else 'code',
                            'function': current_function
                        })
                        current_chunk = []
                        current_function = None
            
            # Add remaining chunk
            if current_chunk:
                chunks.append({
                    'content': '\n'.join(current_chunk),
                    'type': 'function' if current_function else 'code',
                    'function': current_function
                })
        else:
            # For non-Python files, simple line-based chunking
            current_chunk = []
            for line in lines:
                current_chunk.append(line)
                if len('\n'.join(current_chunk)) > chunk_size:
                    chunks.append({
                        'content': '\n'.join(current_chunk),
                        'type': 'text'
                    })
                    current_chunk = []
            if current_chunk:
                chunks.append({
                    'content': '\n'.join(current_chunk),
                    'type': 'text'
                })
        
        return chunks
    
    def index_codebase(self, max_files: Optional[int] = None) -> Dict[str, Any]:
        """Index the entire codebase"""
        indexed_count = 0
        total_size = 0
        
        for file_path in self.root_path.rglob('*'):
            if not file_path.is_file():
                continue
            
            if not self.should_index_file(file_path):
                continue
            
            if max_files and indexed_count >= max_files:
                break
            
            try:
                # Read file content
                content = file_path.read_text(encoding='utf-8', errors='ignore')
                total_size += len(content)
                
                # Extract metadata
                metadata = self.extract_code_metadata(content, file_path)
                
                # Chunk the file
                chunks = self.chunk_code_file(content, file_path)
                
                # Store indexed file
                self.indexed_files[str(file_path.relative_to(self.root_path))] = {
                    'metadata': metadata,
                    'chunks': chunks,
                    'full_content': content[:5000]  # Store first 5KB for quick reference
                }
                
                indexed_count += 1
                
            except Exception as e:
                logger.warning(f"Failed to index {file_path}: {e}")
        
        logger.info(f"Indexed {indexed_count} files ({total_size / 1024:.1f} KB)")
        
        return {
            'files_indexed': indexed_count,
            'total_size_kb': total_size / 1024,
            'indexed_files': list(self.indexed_files.keys())
        }
    
    def search_codebase(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Enhanced text-based search with better matching"""
        query_lower = query.lower()
        query_words = query_lower.split()
        results = []
        
        # If no files indexed yet, return empty (caller should handle gracefully)
        if not self.indexed_files:
            return []
        
        for file_path, file_data in self.indexed_files.items():
            score = 0
            matches = []
            
            # Check file name (exact match gets higher score)
            if query_lower in file_path.lower():
                score += 15
                matches.append(f"Filename match: {file_path}")
            # Partial word matches in filename
            elif any(word in file_path.lower() for word in query_words if len(word) > 3):
                score += 5
                matches.append(f"Filename partial: {file_path}")
            
            # Check functions/classes
            for func in file_data['metadata'].get('functions', []):
                if query_lower in func.lower():
                    score += 8
                    matches.append(f"Function: {func}")
                elif any(word in func.lower() for word in query_words if len(word) > 3):
                    score += 3
                    matches.append(f"Function partial: {func}")
            
            for cls in file_data['metadata'].get('classes', []):
                if query_lower in cls.lower():
                    score += 8
                    matches.append(f"Class: {cls}")
                elif any(word in cls.lower() for word in query_words if len(word) > 3):
                    score += 3
                    matches.append(f"Class partial: {cls}")
            
            # Check content (more sophisticated matching)
            content_lower = file_data['full_content'].lower()
            # Exact phrase match
            if query_lower in content_lower:
                score += 5
                idx = content_lower.find(query_lower)
                if idx >= 0:
                    start = max(0, idx - 200)
                    end = min(len(content_lower), idx + len(query) + 200)
                    context = file_data['full_content'][start:end]
                    matches.append(f"Content exact: ...{context}...")
            # Word matches (multiple words = higher score)
            word_matches = sum(1 for word in query_words if len(word) > 3 and word in content_lower)
            if word_matches > 0:
                score += word_matches * 2
                # Find first match for context
                for word in query_words:
                    if len(word) > 3 and word in content_lower:
                        idx = content_lower.find(word)
                        if idx >= 0:
                            start = max(0, idx - 150)
                            end = min(len(content_lower), idx + len(word) + 150)
                            context = file_data['full_content'][start:end]
                            matches.append(f"Content word '{word}': ...{context}...")
                            break
            
            # Boost score for important files (main.py, routes, core modules)
            if any(important in file_path.lower() for important in ['main.py', 'routes', 'core/', 'api/']):
                score += 3
            
            if score > 0:
                results.append({
                    'file_path': file_path,
                    'score': score,
                    'metadata': file_data['metadata'],
                    'matches': matches,
                    'preview': file_data['full_content'][:1000]  # More preview content
                })
        
        # Sort by score and return top results
        results.sort(key=lambda x: x['score'], reverse=True)
        return results[:limit]
    
    def get_file_content(self, file_path: str) -> Optional[str]:
        """Get full content of an indexed file"""
        full_path = self.root_path / file_path
        if full_path.exists() and full_path.is_file():
            try:
                return full_path.read_text(encoding='utf-8', errors='ignore')
            except Exception as e:
                logger.error(f"Failed to read {file_path}: {e}")
        return None
    
    def get_system_overview(self) -> Dict[str, Any]:
        """Get an overview of the system architecture"""
        overview = {
            'total_files': len(self.indexed_files),
            'python_files': 0,
            'config_files': 0,
            'main_modules': [],
            'key_features': []
        }
        
        for file_path, file_data in self.indexed_files.items():
            if file_path.endswith('.py'):
                overview['python_files'] += 1
                # Identify main modules
                if 'main.py' in file_path or 'app.py' in file_path:
                    overview['main_modules'].append(file_path)
                # Extract key features from metadata
                metadata = file_data.get('metadata', {})
                if metadata.get('functions'):
                    overview['key_features'].extend([f"{file_path}:{func}" for func in metadata['functions'][:3]])
            elif file_path.endswith(('.yaml', '.yml', '.json', '.toml', '.ini')):
                overview['config_files'] += 1
        
        return overview

