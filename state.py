"""Small durable store; one application process owns a state directory."""

from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import sqlite3


ACTIVE_STATES = ("starting", "downloading", "recording", "stopping", "finalizing")


class StateStore:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.owner = (directory / "owner.lock").open("a+b")
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.owner.close()
            raise RuntimeError("This state directory is already in use. Run one app process per state directory.") from None
        self.path = directory / "state.sqlite3"
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, url TEXT NOT NULL,
                    status TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_url ON jobs(url)
                    WHERE status IN ('starting', 'downloading', 'recording', 'stopping', 'finalizing');
                CREATE TABLE IF NOT EXISTS requests (
                    key TEXT PRIMARY KEY, url TEXT NOT NULL, job_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def job_data(job):
        return json.dumps({key: value for key, value in job.items() if key != "worker"})

    def save_job(self, job, *, request_key=None):
        with self.connection() as db:
            db.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET status=excluded.status, data=excluded.data",
                (job["id"], job["url"], job["status"], self.job_data(job)),
            )
            if request_key:
                db.execute("INSERT INTO requests VALUES (?, ?, ?)", (request_key, job["url"], job["id"]))

    def link_request(self, key, job):
        with self.connection() as db:
            db.execute("INSERT INTO requests VALUES (?, ?, ?)", (key, job["url"], job["id"]))

    def request_job(self, key):
        with self.connection() as db:
            row = db.execute(
                "SELECT jobs.data FROM requests JOIN jobs ON jobs.id = requests.job_id WHERE requests.key = ?", (key,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def load_jobs(self):
        with self.connection() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT data FROM jobs ORDER BY rowid DESC")]

    def load_tasks(self):
        with self.connection() as db:
            return {row[0]: json.loads(row[1]) for row in db.execute("SELECT id, data FROM tasks ORDER BY rowid")}

    def save_task(self, task):
        with self.connection() as db:
            db.execute(
                "INSERT INTO tasks VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (task["id"], json.dumps(task)),
            )

    def delete_task(self, task_id):
        with self.connection() as db:
            db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    def close(self):
        # Workers inherit this descriptor. Do not explicitly unlock: their
        # ownership must outlive a failed parent until media finalization ends.
        self.owner.close()
