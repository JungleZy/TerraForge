"""
Database migration: Add time tracking support

Adds:
1. total_running_seconds column to tasks table
2. task_time_records table for tracking start/pause/resume events
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
import logging
from database import get_connection

logger = logging.getLogger(__name__)


def migrate():
    """
    Apply migration to add time tracking support
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 1. Add total_running_seconds column to tasks table
        try:
            cursor.execute('''
                ALTER TABLE tasks
                ADD COLUMN total_running_seconds INTEGER DEFAULT 0
            ''')
            logger.info("Added total_running_seconds column to tasks table")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower():
                logger.info("Column total_running_seconds already exists")
            else:
                raise

        # 2. Create task_time_records table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS task_time_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('start', 'pause', 'resume', 'complete')),
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
            )
        ''')
        logger.info("Created task_time_records table")

        # 3. Create index for performance
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_task_time_records_task_id
            ON task_time_records(task_id, timestamp DESC)
        ''')
        logger.info("Created index on task_time_records")

        conn.commit()
        logger.info("Migration completed successfully")

    except Exception as e:
        conn.rollback()
        logger.error(f"Migration failed: {e}")
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    migrate()
