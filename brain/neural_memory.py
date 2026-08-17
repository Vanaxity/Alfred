"""
Neural Memory System - Personal facts about Master Sam stored with vector embeddings.
Replaces Supabase neural_memories table with local FAISS + Gemini embeddings.

Stores: preferences, plans, identity facts, birthdays, hates, general facts.
Recalls: semantic search via FAISS similarity.
"""

import json
import os
import time
import requests
import numpy as np
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime

import faiss

DATA_DIR = Path(__file__).parent.parent / "data"
INDEX_PATH = DATA_DIR / "neural_memories.faiss"
MEMORY_PATH = DATA_DIR / "neural_memories.json"
BACKUP_DIR = DATA_DIR.parent / "brain" / "data"

EMBEDDING_DIM = 3072  # Gemini embedding-001 dimension


class NeuralMemory:
    """Local neural memory system using FAISS + Gemini embeddings."""

    def __init__(self, google_api_key: str = None):
        self.google_key = google_api_key or os.environ.get("GOOGLE_API_KEY", "")
        self.embed_url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent"
        
        self._memories: List[Dict] = []  # All memories with metadata
        self._index: Optional[faiss.Index] = None  # FAISS index
        self._loaded = False
        
        self._load_or_init()

    def _load_or_init(self):
        """Load existing index from disk or initialize new one."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        
        if INDEX_PATH.exists() and MEMORY_PATH.exists():
            # Load existing
            self._index = faiss.read_index(str(INDEX_PATH))
            with open(MEMORY_PATH, "r", encoding="utf-8") as f:
                self._memories = json.load(f)
            self._loaded = True
            print(f"[NeuralMemory] Loaded {len(self._memories)} memories from disk")
        else:
            # Initialize empty FAISS index
            self._index = faiss.IndexFlatIP(EMBEDDING_DIM)  # Inner product (cosine similarity)
            faiss.normalize_L2(self._index.xb) if hasattr(self._index, 'xb') else None
            self._loaded = False
            print("[NeuralMemory] Initialized new empty memory index")

    def _embed(self, text: str) -> Optional[np.ndarray]:
        """Generate embedding using Gemini API."""
        if not self.google_key:
            return None
        try:
            resp = requests.post(
                f"{self.embed_url}?key={self.google_key}",
                json={"content": {"parts": [{"text": text}]}},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                embedding = data["embedding"]["values"]
                return np.array([embedding], dtype=np.float32)
            else:
                print(f"[NeuralMemory] Embedding failed: {resp.status_code} {resp.text[:100]}")
                return None
        except Exception as e:
            print(f"[NeuralMemory] Embedding error: {e}")
            return None

    def add(self, content: str, category: str = "general", metadata: Dict = None) -> bool:
        """Store a new memory with embedding."""
        embedding = self._embed(content)
        if embedding is None:
            return False
        
        # Normalize for cosine similarity
        faiss.normalize_L2(embedding)
        
        # Add to FAISS index
        self._index.add(embedding)
        
        # Add to memory list
        memory = {
            "id": f"mem_{int(time.time() * 1000)}",
            "content": content,
            "category": category,
            "metadata": metadata or {},
            "created_at": datetime.now().isoformat(),
        }
        self._memories.append(memory)
        
        self._save()
        return True

    def recall(self, query: str, top_k: int = 5, min_similarity: float = 0.3) -> List[Dict]:
        """Semantic search: find most relevant memories."""
        if self._index.ntotal == 0:
            return []
        
        embedding = self._embed(query)
        if embedding is None:
            return []
        
        faiss.normalize_L2(embedding)
        
        k = min(top_k, self._index.ntotal)
        scores, indices = self._index.search(embedding.astype(np.float32), k)
        
        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0 or idx >= len(self._memories):
                continue
            if float(score) < min_similarity:
                continue
            memory = self._memories[idx].copy()
            memory["similarity"] = float(score)
            results.append(memory)
        
        return results

    def search_by_category(self, category: str, top_k: int = 10) -> List[Dict]:
        """Get all memories of a specific category."""
        return [m for m in self._memories if m.get("category") == category][:top_k]

    def get_all(self, limit: int = 100) -> List[Dict]:
        """Get all memories (recent first)."""
        return list(reversed(self._memories))[:limit]

    def count(self) -> int:
        return self._index.ntotal

    def _save(self):
        """Persist index and memories to disk."""
        faiss.write_index(self._index, str(INDEX_PATH))
        with open(MEMORY_PATH, "w", encoding="utf-8") as f:
            json.dump(self._memories, f, indent=2, ensure_ascii=False)

    def import_supabase_export(self, json_path: Path = None):
        """Import neural memories from Supabase JSON export."""
        path = json_path or (BACKUP_DIR / "neural_memories.json")
        if not path.exists():
            print(f"[NeuralMemory] No export found at {path}")
            return 0
        
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        count = 0
        for item in data:
            content = item.get("content", "")
            if not content:
                continue
            # Check if already exists
            if any(m["content"] == content for m in self._memories):
                continue
            
            embedding = self._embed(content)
            if embedding is None:
                continue
            
            faiss.normalize_L2(embedding)
            self._index.add(embedding)
            
            self._memories.append({
                "id": item.get("id", f"mem_{int(time.time() * 1000)}"),
                "content": content,
                "category": item.get("category", "general"),
                "metadata": item.get("metadata", {}),
                "created_at": item.get("created_at", datetime.now().isoformat()),
            })
            count += 1
        
        if count > 0:
            self._save()
            print(f"[NeuralMemory] Imported {count} memories from Supabase export")
        
        return count


# Singleton
_instance: Optional[NeuralMemory] = None

def get_neural_memory() -> NeuralMemory:
    global _instance
    if _instance is None:
        _instance = NeuralMemory()
    return _instance
