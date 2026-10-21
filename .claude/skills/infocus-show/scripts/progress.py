"""Tells the worker which step the unattended edit is on, so the Portal can show it.

    python progress.py <stage> "<message>" [--job <job_dir>]
Writes <job_dir>/status.json as {"stage", "message", "at"} atomically (temp file, then rename), so the worker never
reads half a file. The job folder defaults to $INFOCUS_JOB_DIR, else the current directory. Stdlib only.
"""
import datetime
import json
import os
import sys
import tempfile

STAGES = ("analyze", "graphics", "audio", "build", "render", "check", "fix")


def write_status(job_dir, stage, message):
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {', '.join(STAGES)}")
    body = {"stage": stage, "message": message[:300],
            "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    fd, tmp = tempfile.mkstemp(prefix=".status-", suffix=".json", dir=job_dir)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(body, f)
        os.replace(tmp, os.path.join(job_dir, "status.json"))
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return body


def main(argv):
    job_dir = os.environ.get("INFOCUS_JOB_DIR") or os.getcwd()
    if "--job" in argv:
        k = argv.index("--job")
        job_dir = argv[k + 1]
        argv = argv[:k] + argv[k + 2:]
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        write_status(job_dir, argv[0], argv[1])
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
