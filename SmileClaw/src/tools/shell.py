import subprocess

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


def run(command):

    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True
    )

    return result.stdout