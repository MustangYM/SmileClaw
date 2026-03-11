import json
from datetime import datetime, timezone
import uuid
from pathlib import Path

from security.policy import ShellPolicyEngine


class ApprovalManager:

    def __init__(self, workspace_root):
        self.workspace_root = Path(workspace_root).resolve()
        self.session_allowed_roots = set()
        self.pending = {}
        self.policy_path = self.workspace_root / ".smileclaw" / "exec-approvals.json"
        self.rules = []
        self.policy_engine = ShellPolicyEngine(self.workspace_root)
        self._load_policy()

    def _now_iso(self):
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _new_rule_id(self):
        return f"rule_{uuid.uuid4().hex[:8]}"

    def _normalize_rule(self, rule):
        scope_type = rule.get("scope_type")
        scope_value = rule.get("scope_value")
        tool = rule.get("tool", "shell")
        decision = rule.get("decision", "allow")
        expires_at = rule.get("expires_at")
        created_at = rule.get("created_at") or self._now_iso()
        rule_id = rule.get("id") or self._new_rule_id()

        if scope_type == "path_root" and scope_value:
            scope_value = str(Path(scope_value).resolve(strict=False))

        return {
            "id": rule_id,
            "tool": tool,
            "scope_type": scope_type,
            "scope_value": scope_value,
            "decision": decision,
            "created_at": created_at,
            "expires_at": expires_at
        }

    def _is_rule_expired(self, rule):
        expires_at = rule.get("expires_at")
        if not expires_at:
            return False
        try:
            if expires_at.endswith("Z"):
                expires_at = expires_at.replace("Z", "+00:00")
            exp_dt = datetime.fromisoformat(expires_at)
            return datetime.now(timezone.utc) >= exp_dt.astimezone(timezone.utc)
        except ValueError:
            return False

    def _active_rules(self):
        return [rule for rule in self.rules if not self._is_rule_expired(rule)]

    def _is_sub_path(self, target, root):
        try:
            target.relative_to(root)
            return True
        except ValueError:
            return False

    def _load_policy(self):
        if not self.policy_path.exists():
            return

        try:
            data = json.loads(self.policy_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return

        # v1.2 schema
        if isinstance(data, dict) and "rules" in data:
            for item in data.get("rules", []):
                if not isinstance(item, dict):
                    continue
                normalized = self._normalize_rule(item)
                if normalized["scope_type"] and normalized["scope_value"]:
                    self.rules.append(normalized)
            return

        # backward compatibility: legacy schema
        legacy_roots = data.get("always_allowed_roots", []) if isinstance(data, dict) else []
        for root in legacy_roots:
            self.rules.append(
                self._normalize_rule(
                    {
                        "tool": "shell",
                        "scope_type": "path_root",
                        "scope_value": root,
                        "decision": "allow",
                        "created_at": self._now_iso(),
                        "expires_at": None
                    }
                )
            )

    def _save_policy(self):
        self.policy_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "rules": self._active_rules()
        }
        self.policy_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    def _allowed_roots(self):
        roots = [str(self.workspace_root)]
        roots.extend(sorted(self.session_allowed_roots))
        for rule in self._active_rules():
            if (
                rule.get("tool") == "shell"
                and rule.get("scope_type") == "path_root"
                and rule.get("decision") == "allow"
            ):
                roots.append(rule["scope_value"])
        # keep order while removing duplicates
        seen = set()
        result = []
        for item in roots:
            norm = str(Path(item).resolve(strict=False))
            if norm in seen:
                continue
            seen.add(norm)
            result.append(norm)
        return result

    def _match_path_rule(self, scope_value, all_paths):
        root = Path(scope_value).resolve(strict=False)
        for raw_path in all_paths:
            target = Path(raw_path).resolve(strict=False)
            if self._is_sub_path(target, root):
                return True
        return False

    def _find_matching_rules(self, tool, command_hash, all_paths, decision):
        matched = []
        for rule in self._active_rules():
            if rule.get("tool") != tool or rule.get("decision") != decision:
                continue

            scope_type = rule.get("scope_type")
            scope_value = rule.get("scope_value")
            if not scope_type or not scope_value:
                continue

            if scope_type == "command_hash" and scope_value == command_hash:
                matched.append(rule)
                continue

            if scope_type == "path_root" and self._match_path_rule(scope_value, all_paths):
                matched.append(rule)
                continue

        return matched

    def _remember_session_allow(self, restricted_paths):
        for item in restricted_paths:
            path_obj = Path(item).resolve(strict=False)
            if path_obj.exists():
                allow_root = path_obj if path_obj.is_dir() else path_obj.parent
            else:
                allow_root = path_obj.parent if path_obj.suffix else path_obj
            self.session_allowed_roots.add(str(allow_root))

    def _upsert_rule(self, tool, scope_type, scope_value, decision):
        normalized_scope = scope_value
        if scope_type == "path_root":
            normalized_scope = str(Path(scope_value).resolve(strict=False))

        for rule in self.rules:
            if (
                rule.get("tool") == tool
                and rule.get("scope_type") == scope_type
                and rule.get("scope_value") == normalized_scope
                and rule.get("decision") == decision
                and not self._is_rule_expired(rule)
            ):
                return rule

        rule = self._normalize_rule(
            {
                "tool": tool,
                "scope_type": scope_type,
                "scope_value": normalized_scope,
                "decision": decision,
                "created_at": self._now_iso(),
                "expires_at": None
            }
        )
        self.rules.append(rule)
        return rule

    def check_shell_command(self, command):
        evaluation = self.policy_engine.evaluate(
            command=command,
            allowed_roots=self._allowed_roots(),
            denied_commands=set()
        )
        restricted_paths = evaluation["restricted_paths"]
        all_paths = evaluation["normalized"]["normalized_paths"]
        command_hash = evaluation["command_hash"]

        # Priority: explicit deny > exact command allow > path root allow > session allow > default policy
        deny_rules = self._find_matching_rules(
            tool="shell",
            command_hash=command_hash,
            all_paths=all_paths,
            decision="deny"
        )
        if deny_rules:
            return {
                "requires_approval": False,
                "decision": "deny",
                "all_paths": all_paths,
                "restricted_paths": restricted_paths,
                "reason_codes": ["POLICY_EXPLICIT_DENY"],
                "risk_flags": evaluation["risk_flags"],
                "command_hash": command_hash
            }

        command_allow_rules = self._find_matching_rules(
            tool="shell",
            command_hash=command_hash,
            all_paths=all_paths,
            decision="allow"
        )
        command_allow_rules = [
            item for item in command_allow_rules if item.get("scope_type") == "command_hash"
        ]
        if command_allow_rules:
            return {
                "requires_approval": False,
                "decision": "allow",
                "all_paths": all_paths,
                "restricted_paths": [],
                "reason_codes": ["POLICY_COMMAND_ALLOW"],
                "risk_flags": evaluation["risk_flags"],
                "command_hash": command_hash
            }

        if not restricted_paths:
            return {
                "requires_approval": False,
                "decision": evaluation["decision"],
                "all_paths": all_paths,
                "restricted_paths": [],
                "reason_codes": evaluation["reason_codes"],
                "risk_flags": evaluation["risk_flags"],
                "command_hash": command_hash
            }

        approval_id = str(uuid.uuid4())[:8]
        request = {
            "approval_id": approval_id,
            "tool": "shell",
            "command": command,
            "restricted_paths": restricted_paths,
            "command_hash": command_hash
        }
        self.pending[approval_id] = request

        return {
            "requires_approval": True,
            "decision": "require_approval",
            "approval_id": approval_id,
            "all_paths": all_paths,
            "restricted_paths": restricted_paths,
            "reason_codes": evaluation["reason_codes"],
            "risk_flags": evaluation["risk_flags"],
            "command_hash": command_hash
        }

    def resolve(self, approval_id, decision):
        request = self.pending.get(approval_id)
        if not request:
            return {
                "ok": False,
                "reason": "approval_not_found"
            }

        restricted_paths = request.get("restricted_paths", [])

        if decision == "deny":
            del self.pending[approval_id]
            return {
                "ok": True,
                "decision": "deny",
                "request": request,
                "audit_events": []
            }

        if decision in ("allow_once", "allow_always"):
            self._remember_session_allow(restricted_paths)

            audit_events = []
            if decision == "allow_always":
                for item in restricted_paths:
                    path_obj = Path(item).resolve(strict=False)
                    if path_obj.exists():
                        allow_root = path_obj if path_obj.is_dir() else path_obj.parent
                    else:
                        allow_root = path_obj.parent if path_obj.suffix else path_obj
                    created = self._upsert_rule(
                        tool="shell",
                        scope_type="path_root",
                        scope_value=str(allow_root),
                        decision="allow"
                    )
                    audit_events.append(
                        {
                            "type": "approval_rule_created",
                            "rule_id": created["id"]
                        }
                    )

                self._save_policy()

            del self.pending[approval_id]
            return {
                "ok": True,
                "decision": decision,
                "request": request,
                "audit_events": audit_events
            }

        return {
            "ok": False,
            "reason": "invalid_decision"
        }
