"""Operation lock; export jobs also have an account file lock."""
import threading

_job_lock = threading.Lock()
app_state = {
    "logged_in": False,
    "username": None,
    "user_id": None,
    "job_status": "idle",
    "non_followers": [],
    "error": None,
}


def try_start_job():
    return _job_lock.acquire(blocking=False)


def finish_job():
    _job_lock.release()
