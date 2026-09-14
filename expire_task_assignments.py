"""One-minute Task Scheduler job for releasing expired TaskPe assignments."""
import os
import sqlite3


DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'taskpe.db')


def expire_assignments(db_path=DB_PATH):
    """Expire overdue assignments once; safe to call repeatedly."""
    conn = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(task_assignments)")}
        if 'started_at' not in columns:
            conn.execute("ALTER TABLE task_assignments ADD COLUMN started_at DATETIME")
        if 'expires_at' not in columns:
            conn.execute("ALTER TABLE task_assignments ADD COLUMN expires_at DATETIME")
        if 'slot_released' not in columns:
            conn.execute("ALTER TABLE task_assignments ADD COLUMN slot_released INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE task_assignments SET started_at = COALESCE(started_at, assigned_at, CURRENT_TIMESTAMP)")
        conn.execute('''
            UPDATE task_assignments
            SET expires_at = COALESCE(
                expires_at,
                datetime(started_at, '+' || COALESCE(
                    (SELECT time_limit_minutes FROM tasks WHERE tasks.id = task_assignments.task_id),
                    25
                ) || ' minutes')
            )
        ''')
        cursor = conn.execute('''
            UPDATE task_assignments
            SET status = 'expired', slot_released = 1
            WHERE status = 'in_progress'
              AND expires_at IS NOT NULL
              AND expires_at <= CURRENT_TIMESTAMP
              AND slot_released = 0
        ''')
        conn.execute("UPDATE task_assignments SET slot_released = 1 WHERE status IN ('expired', 'cancelled') AND slot_released = 0")
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


if __name__ == '__main__':
    print(f"Released {expire_assignments()} expired task assignment(s).")
