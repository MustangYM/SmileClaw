import hashlib
import re
import shlex
from pathlib import Path


SHELL_SEPARATORS = {"&&", "||", ";", "|"}


def _is_sub_path(target, root):
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


class ShellPolicyEngine:

    def __init__(self, workspace_root):
        self.workspace_root = Path(workspace_root).resolve()

    def _parse(self, command):
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()

        separators = [token for token in tokens if token in SHELL_SEPARATORS]
        subshell = "$(" in command or "`" in command
        redirects = [token for token in tokens if token in (">", ">>")]

        return {
            "tokens": tokens,
            "separators": separators,
            "subshell": subshell,
            "redirects": redirects
        }

    def _normalize(self, parsed):
        tokens = parsed["tokens"]
        cwd = self.workspace_root
        normalized_paths = []
        cd_targets = []
        idx = 0

        while idx < len(tokens):
            token = tokens[idx]

            if token == "cd":
                if idx + 1 >= len(tokens):
                    raw_target = str(Path.home())
                else:
                    raw_target = tokens[idx + 1]

                if raw_target in SHELL_SEPARATORS:
                    idx += 1
                    continue

                if raw_target.startswith("~"):
                    candidate = Path(raw_target).expanduser().resolve(strict=False)
                elif raw_target.startswith("/"):
                    candidate = Path(raw_target).resolve(strict=False)
                else:
                    candidate = (cwd / raw_target).resolve(strict=False)

                cd_targets.append(str(candidate))
                normalized_paths.append(str(candidate))
                cwd = candidate
                idx += 2
                continue

            if token.startswith("~"):
                candidate = Path(token).expanduser().resolve(strict=False)
                normalized_paths.append(str(candidate))
            elif token.startswith("/"):
                normalized_paths.append(str(Path(token).resolve(strict=False)))
            elif token in (".", "..") or token.startswith("../") or token.startswith("./"):
                candidate = (cwd / token).resolve(strict=False)
                normalized_paths.append(str(candidate))

            idx += 1

        return {
            "normalized_paths": normalized_paths,
            "cd_targets": cd_targets
        }

    def _semantic_extract(self, parsed, normalized):
        tokens = parsed["tokens"]
        read_paths = []
        write_paths = []
        delete_paths = []
        network_ops = []

        for idx, token in enumerate(tokens):
            if token in ("curl", "wget", "nc", "ssh"):
                network_ops.append(token)

            if token in ("rm", "unlink") and idx + 1 < len(tokens):
                delete_paths.append(tokens[idx + 1])

            if token in (">", ">>") and idx + 1 < len(tokens):
                write_paths.append(tokens[idx + 1])

        for item in normalized["normalized_paths"]:
            if item not in write_paths and item not in delete_paths:
                read_paths.append(item)

        return {
            "read_paths": read_paths,
            "write_paths": write_paths,
            "delete_paths": delete_paths,
            "network_ops": network_ops
        }

    def evaluate(self, command, allowed_roots, denied_commands):
        parsed = self._parse(command)
        normalized = self._normalize(parsed)
        semantic = self._semantic_extract(parsed, normalized)

        normalized_allowed = [Path(item).resolve(strict=False) for item in allowed_roots]

        restricted_paths = []
        for raw_path in normalized["normalized_paths"]:
            target = Path(raw_path).resolve(strict=False)
            if not any(_is_sub_path(target, root) for root in normalized_allowed):
                restricted_paths.append(raw_path)

        reason_codes = []
        risk_flags = []

        if parsed["separators"]:
            risk_flags.append("multi_segment")
        if "|" in parsed["separators"]:
            risk_flags.append("pipe_chain")
        if parsed["redirects"]:
            risk_flags.append("redirection")
        if parsed["subshell"]:
            risk_flags.append("subshell")
        if semantic["network_ops"]:
            risk_flags.append("network")
        if any(path.startswith(str(self.workspace_root.parent)) for path in normalized["cd_targets"]):
            risk_flags.append("cwd_escape_attempt")

        command_hash = hashlib.sha256(command.encode("utf-8")).hexdigest()
        decision = "allow"

        if command in denied_commands:
            decision = "deny"
            reason_codes.append("DENIED_IN_TURN")
        elif restricted_paths:
            decision = "require_approval"
            reason_codes.append("OUTSIDE_WORKSPACE")

        return {
            "decision": decision,
            "reason_codes": reason_codes,
            "restricted_paths": restricted_paths,
            "risk_flags": risk_flags,
            "parsed": parsed,
            "normalized": normalized,
            "semantic": semantic,
            "command_hash": command_hash
        }
