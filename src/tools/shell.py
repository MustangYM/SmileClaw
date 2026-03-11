import subprocess
import shlex
import re
import time

SCHEMA = {
    "name": "shell",
    "description": "Run a shell command on the system",
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to run"
            }
        },
        "required": ["command"]
    }
}


PERMISSION_PATTERNS = [
    "permission denied",
    "operation not permitted",
    "eacces",
    "eperm"
]

COMMAND_TIMEOUT_SECONDS = 15
MAX_COMMAND_LENGTH = 2000
MAX_OUTPUT_CHARS = 8000


def extract_absolute_paths(command):
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    paths = []
    for token in tokens:
        if token.startswith("/"):
            path = token.rstrip(";,)")
            if path and path not in paths:
                paths.append(path)
            continue

        for match in re.findall(r"(/[^ \t\n\r\f\v\"'|;]+)", token):
            path = match.rstrip(";,)")
            if path and path not in paths:
                paths.append(path)

    return paths


def _looks_like_permission_error(stderr_text):
    lower_text = (stderr_text or "").lower()
    return any(pattern in lower_text for pattern in PERMISSION_PATTERNS)


def _truncate_text(text):
    text = text or ""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    truncated = text[:MAX_OUTPUT_CHARS] + "\n...[output truncated]"
    return truncated, True


def _decode_output(data):
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def run(command):
    if len(command) > MAX_COMMAND_LENGTH:
        return {
            "command": command,
            "stdout": "",
            "stderr": "Command exceeds maximum allowed length.",
            "exit_code": -1,
            "ok": False,
            "needs_system_permission": False,
            "paths": extract_absolute_paths(command),
            "duration_ms": 0,
            "output_truncated": False,
            "error_code": "COMMAND_TOO_LONG"
        }

    start = time.time()
    timeout_hit = False
    error_code = None

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=False,
            timeout=COMMAND_TIMEOUT_SECONDS
        )
        raw_stdout = _decode_output(result.stdout)
        raw_stderr = _decode_output(result.stderr)
        exit_code = result.returncode
    except subprocess.TimeoutExpired as exc:
        timeout_hit = True
        raw_stdout = _decode_output(exc.stdout)
        raw_stderr = _decode_output(exc.stderr) + "\nCommand timed out."
        exit_code = -1
        error_code = "TIMEOUT"

    duration_ms = int((time.time() - start) * 1000)
    stdout, trunc_stdout = _truncate_text(raw_stdout.strip())
    stderr, trunc_stderr = _truncate_text(raw_stderr.strip())
    output_truncated = trunc_stdout or trunc_stderr

    return {
        "command": command,
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "ok": exit_code == 0 and not timeout_hit,
        "needs_system_permission": _looks_like_permission_error(stderr),
        "paths": extract_absolute_paths(command),
        "duration_ms": duration_ms,
        "output_truncated": output_truncated,
        "error_code": error_code
    }
