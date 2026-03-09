"""
Persistent Feedback Store for DSPy Prompt Optimization.

Persists technique performance feedback to SQLite so the DSPy optimizer
retains its learned preferences across application restarts.

Why persistent feedback for Spark/Synapse/Kusto troubleshooting:
- The DSPy optimizer learns which prompting technique works best for
  each error class (e.g., few-shot for OOM, CoT for timeouts).
  Without persistence, this knowledge is lost on every restart.
- Support team upvotes/downvotes on AI responses are valuable training
  signal — losing them on restart wastes the team's effort.
- Over weeks of production use, the optimizer accumulates enough data
  to make statistically significant technique selections. Persistence
  ensures this data survives deployments and maintenance windows.
- Enables A/B testing across deployments: compare technique performance
  from release N to release N+1 using the same historical data.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import structlog

logger = structlog.get_logger(__name__)

DEFAULT_DB_PATH = "data/feedback.db"


class FeedbackStore:
    """
    SQLite-backed store for DSPy feedback and technique metrics.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._create_tables()
        logger.info("feedback_store_initialized", db_path=db_path)

    def _create_tables(self) -> None:
        """Create the feedback tables if they don't exist."""
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS technique_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                error_class TEXT NOT NULL,
                technique TEXT NOT NULL,
                score REAL NOT NULL,
                session_id TEXT,
                alert_id TEXT,
                metadata TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS optimal_signatures (
                error_class TEXT PRIMARY KEY,
                technique TEXT NOT NULL,
                temperature REAL DEFAULT 0.1,
                avg_score REAL,
                sample_count INTEGER DEFAULT 0,
                reason TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_feedback_error_class
            ON technique_feedback(error_class);

            CREATE INDEX IF NOT EXISTS idx_feedback_technique
            ON technique_feedback(error_class, technique);
            """
        )
        self._conn.commit()

    def record_feedback(
        self,
        error_class: str,
        technique: str,
        score: float,
        session_id: str = "",
        alert_id: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """Record a technique feedback entry."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO technique_feedback
            (error_class, technique, score, session_id, alert_id, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                error_class,
                technique,
                score,
                session_id,
                alert_id,
                json.dumps(metadata) if metadata else None,
                now,
            ),
        )
        self._conn.commit()

        # Check if we should update the optimal signature
        self._maybe_update_signature(error_class)

    def _maybe_update_signature(self, error_class: str, min_samples: int = 5) -> None:
        """Update optimal signature if enough feedback exists."""
        cursor = self._conn.execute(
            """
            SELECT technique, AVG(score) as avg_score, COUNT(*) as cnt
            FROM technique_feedback
            WHERE error_class = ?
            GROUP BY technique
            HAVING cnt >= ?
            ORDER BY avg_score DESC
            LIMIT 1
            """,
            (error_class, min_samples),
        )
        row = cursor.fetchone()
        if row:
            technique, avg_score, count = row
            now = datetime.now(timezone.utc).isoformat()
            self._conn.execute(
                """
                INSERT OR REPLACE INTO optimal_signatures
                (error_class, technique, avg_score, sample_count, reason, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    error_class,
                    technique,
                    round(avg_score, 4),
                    count,
                    f"Auto-optimized: avg {avg_score:.2f} over {count} samples",
                    now,
                ),
            )
            self._conn.commit()
            logger.info(
                "optimal_signature_persisted",
                error_class=error_class,
                technique=technique,
                avg_score=round(avg_score, 4),
                samples=count,
            )

    def load_signatures(self) -> dict[str, dict[str, Any]]:
        """Load all persisted optimal signatures."""
        cursor = self._conn.execute(
            "SELECT error_class, technique, temperature, avg_score, sample_count, reason FROM optimal_signatures"
        )
        signatures = {}
        for row in cursor.fetchall():
            error_class, technique, temperature, avg_score, count, reason = row
            signatures[error_class] = {
                "technique": technique,
                "temperature": temperature or 0.1,
                "reason": reason or f"Persisted: avg {avg_score} over {count} samples",
            }
        logger.info("signatures_loaded_from_db", count=len(signatures))
        return signatures

    def get_feedback_summary(self) -> list[dict[str, Any]]:
        """Get a summary of feedback by error class and technique."""
        cursor = self._conn.execute(
            """
            SELECT error_class, technique, AVG(score), COUNT(*),
                   MIN(score), MAX(score)
            FROM technique_feedback
            GROUP BY error_class, technique
            ORDER BY error_class, AVG(score) DESC
            """
        )
        results = []
        for row in cursor.fetchall():
            results.append({
                "error_class": row[0],
                "technique": row[1],
                "avg_score": round(row[2], 3),
                "count": row[3],
                "min_score": round(row[4], 3),
                "max_score": round(row[5], 3),
            })
        return results

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
