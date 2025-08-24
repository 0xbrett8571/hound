"""
Concurrent knowledge management system for multi-process graph and hypothesis operations.

This module provides thread-safe, file-based storage for knowledge graphs and 
vulnerability hypotheses, allowing multiple agents to collaborate on analysis.
"""

import json
import fcntl
import time
import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, asdict, field
from datetime import datetime
from enum import Enum
from abc import ABC, abstractmethod


# ============================================================================
# Base Concurrent Store
# ============================================================================

class ConcurrentFileStore(ABC):
    """Base class for file-based storage with process-safe locking."""
    
    def __init__(self, file_path: Path, agent_id: Optional[str] = None):
        self.file_path = Path(file_path)
        self.agent_id = agent_id or "anonymous"
        self.lock_path = self.file_path.with_suffix('.lock')
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        
        if not self.file_path.exists():
            self._save_data(self._get_empty_data())
    
    @abstractmethod
    def _get_empty_data(self) -> Dict:
        """Return initial empty data structure."""
        pass
    
    def _acquire_lock(self, timeout: float = 10.0) -> Any:
        """Acquire exclusive lock on storage file."""
        start_time = time.time()
        lock_file = open(self.lock_path, 'w')
        
        while True:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return lock_file
            except IOError:
                if time.time() - start_time > timeout:
                    lock_file.close()
                    raise TimeoutError(f"Lock timeout: {self.file_path}")
                time.sleep(0.05)
    
    def _release_lock(self, lock_file: Any):
        """Release file lock."""
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
            lock_file.close()
            if self.lock_path.exists():
                self.lock_path.unlink()
        except:
            pass
    
    def _load_data(self) -> Dict:
        """Load data from file."""
        try:
            with open(self.file_path, 'r') as f:
                return json.load(f)
        except:
            return self._get_empty_data()
    
    def _save_data(self, data: Dict):
        """Save data atomically using a unique temp file to avoid races."""
        import tempfile
        # Create temp file in same directory for atomic replace on same filesystem
        with tempfile.NamedTemporaryFile('w', dir=str(self.file_path.parent), prefix=self.file_path.stem + '.', suffix='.tmp', delete=False) as tf:
            json.dump(data, tf, indent=2, default=str)
            tmp_name = Path(tf.name)
        tmp_name.replace(self.file_path)
    
    def update_atomic(self, update_func) -> Any:
        """Atomically read, update, and write data."""
        lock = self._acquire_lock()
        try:
            data = self._load_data()
            updated_data, result = update_func(data)
            if updated_data is not None:
                self._save_data(updated_data)
            return result
        finally:
            self._release_lock(lock)


# ============================================================================
# Hypothesis Data Structures  
# ============================================================================

class HypothesisStatus(Enum):
    PROPOSED = "proposed"
    INVESTIGATING = "investigating"
    SUPPORTED = "supported"
    REFUTED = "refuted"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


@dataclass
class Evidence:
    """Evidence for/against a hypothesis."""
    description: str
    type: str  # supports/refutes/related
    confidence: float = 0.7
    node_refs: List[str] = field(default_factory=list)
    created_by: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class Hypothesis:
    """Vulnerability hypothesis."""
    title: str
    description: str
    vulnerability_type: str
    severity: str  # low/medium/high/critical
    confidence: float = 0.5
    status: str = "proposed"
    node_refs: List[str] = field(default_factory=list)
    evidence: List[Evidence] = field(default_factory=list)
    reasoning: str = ""
    properties: Dict[str, Any] = field(default_factory=dict)  # Store graph name, etc.
    created_by: Optional[str] = None
    reported_by_model: Optional[str] = None  # Track which LLM model first reported this
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    id: str = ""
    
    def __post_init__(self):
        if not self.id:
            content = f"{self.title}{self.vulnerability_type}{''.join(self.node_refs)}"
            self.id = f"hyp_{hashlib.md5(content.encode()).hexdigest()[:12]}"


# ============================================================================
# Hypothesis Store
# ============================================================================

class HypothesisStore(ConcurrentFileStore):
    """Manages vulnerability hypotheses with concurrent access."""
    
    def _get_empty_data(self) -> Dict:
        return {
            "version": "1.0",
            "hypotheses": {},
            "metadata": {
                "total": 0,
                "confirmed": 0,
                "last_modified": datetime.now().isoformat()
            }
        }
    
    def propose(self, hypothesis: Hypothesis) -> Tuple[bool, str]:
        """Propose a new hypothesis with improved duplicate detection."""
        def update(data):
            hypotheses = data["hypotheses"]
            
            # Improved duplicate check with similarity detection
            for h_id, h in hypotheses.items():
                # Check exact title match (case-insensitive)
                if h["title"].lower() == hypothesis.title.lower():
                    return data, (False, f"Duplicate title: {h_id}")
                
                # Check for similar hypotheses (same vulnerability type and nodes)
                if (h.get("vulnerability_type", "").lower() == hypothesis.vulnerability_type.lower() and
                    set(h.get("node_refs", [])) & set(hypothesis.node_refs)):  # Overlapping nodes
                    # Check description similarity
                    existing_desc = h.get("description", "").lower()
                    new_desc = hypothesis.description.lower()
                    
                    # Simple similarity check - if key terms overlap significantly
                    existing_terms = set(existing_desc.split())
                    new_terms = set(new_desc.split())
                    
                    # Remove common words
                    common_words = {'the', 'a', 'an', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 
                                   'by', 'from', 'as', 'is', 'was', 'are', 'were', 'be', 'been',
                                   'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will',
                                   'would', 'could', 'should', 'may', 'might', 'must', 'can', 'that',
                                   'this', 'these', 'those', 'and', 'or', 'but', 'if', 'because'}
                    
                    existing_terms = existing_terms - common_words
                    new_terms = new_terms - common_words
                    
                    if existing_terms and new_terms:
                        overlap = len(existing_terms & new_terms)
                        similarity = overlap / min(len(existing_terms), len(new_terms))
                        
                        if similarity > 0.6:  # 60% similarity threshold
                            return data, (False, f"Similar to existing: {h_id}")
            
            hypothesis.created_by = self.agent_id
            hypotheses[hypothesis.id] = asdict(hypothesis)
            data["metadata"]["total"] = len(hypotheses)
            data["metadata"]["last_modified"] = datetime.now().isoformat()
            
            return data, (True, hypothesis.id)
        
        return self.update_atomic(update)
    
    def add_evidence(self, hypothesis_id: str, evidence: Evidence) -> bool:
        """Add evidence to a hypothesis."""
        def update(data):
            if hypothesis_id not in data["hypotheses"]:
                return data, False
            
            hyp = data["hypotheses"][hypothesis_id]
            evidence.created_by = self.agent_id
            hyp["evidence"].append(asdict(evidence))
            
            # Auto-adjust status
            supporting = sum(1 for e in hyp["evidence"] if e["type"] == "supports")
            refuting = sum(1 for e in hyp["evidence"] if e["type"] == "refutes")
            
            if refuting > supporting * 2:
                hyp["status"] = "refuted"
            elif supporting > 3:
                hyp["status"] = "supported"
            elif supporting > 0:
                hyp["status"] = "investigating"
            
            return data, True
        
        return self.update_atomic(update)
    
    def adjust_confidence(self, hypothesis_id: str, confidence: float, reason: str) -> bool:
        """Adjust hypothesis confidence."""
        def update(data):
            if hypothesis_id not in data["hypotheses"]:
                return data, False
            
            hyp = data["hypotheses"][hypothesis_id]
            hyp["confidence"] = confidence
            
            # Auto-update status (analysis agent can only reject, not confirm)
            # Only the finalize agent can set status to "confirmed"
            if confidence <= 0.1:
                hyp["status"] = "rejected"
            
            return data, True
        
        return self.update_atomic(update)
    
    def get_by_node(self, node_id: str) -> List[Dict]:
        """Get hypotheses for a node."""
        lock = self._acquire_lock()
        try:
            data = self._load_data()
            return [h for h in data["hypotheses"].values() if node_id in h.get("node_refs", [])]
        finally:
            self._release_lock(lock)


# ============================================================================
# Graph Store
# ============================================================================

class GraphStore(ConcurrentFileStore):
    """Manages graph files with concurrent access."""
    
    def _get_empty_data(self) -> Dict:
        return {
            "name": self.file_path.stem,
            "created_at": datetime.now().isoformat(),
            "nodes": [],
            "edges": [],
            "metadata": {"version": "1.0"}
        }
    
    def save_graph(self, graph_data: Dict) -> bool:
        """Save entire graph data atomically."""
        def update(data):
            # Replace entire graph data
            return graph_data, True
        
        return self.update_atomic(update)
    
    def load_graph(self) -> Dict:
        """Load graph data with shared lock."""
        lock = self._acquire_lock()
        try:
            return self._load_data()
        finally:
            self._release_lock(lock)
    
    def update_nodes(self, node_updates: List[Dict]) -> bool:
        """Update specific nodes in the graph."""
        def update(data):
            # Create a map of node IDs to updates
            update_map = {update['id']: update for update in node_updates}
            
            # Update existing nodes
            for i, node in enumerate(data.get('nodes', [])):
                if node['id'] in update_map:
                    # Merge updates into existing node
                    node.update(update_map[node['id']])
            
            return data, True
        
        return self.update_atomic(update)

