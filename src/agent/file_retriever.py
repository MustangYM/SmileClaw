import json
import re
import shlex
import subprocess
import zipfile
from xml.etree import ElementTree
from pathlib import Path


FILE_SEARCH_HINTS = (
    "找", "查找", "搜索", "查一下", "查找文件", "找文件", "文稿", "文件",
    "find", "search", "locate", "file", "document", "docs"
)

DEFAULT_DIRECTORIES = ["~/Documents", "~/Desktop", "~/Downloads"]
TEXT_EXTENSIONS = {"txt", "md", "rtf", "log", "csv"}
CONTENT_SCAN_LIMIT = 20
MAX_FILE_READ_BYTES = 1024 * 256


class RetrieverError(Exception):
    pass


def _extract_json_object(text: str) -> dict | None:
    raw = (text or "").strip()
    if not raw:
        return None

    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    first = raw.find("{")
    last = raw.rfind("}")
    if first >= 0 and last > first:
        maybe = raw[first:last + 1]
        try:
            obj = json.loads(maybe)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            return None

    return None


def _uniq_strings(values: list[str], max_items: int = 8) -> list[str]:
    seen = set()
    output = []
    for item in values:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        output.append(text)
        if len(output) >= max_items:
            break
    return output


def _normalize_directories(values: list[str]) -> list[str]:
    dirs = []
    for item in values:
        raw = str(item).strip()
        if not raw:
            continue
        if raw.startswith("~"):
            path = str(Path(raw).expanduser())
        else:
            path = raw
        dirs.append(path)
    return _uniq_strings(dirs, max_items=5)


def _build_round1_command(directories: list[str], keywords: list[str], extensions: list[str]) -> str:
    quoted_dirs = " ".join(shlex.quote(d) for d in directories)
    kw_expr = " -o ".join(f"-iname {shlex.quote(f'*{kw}*')}" for kw in keywords)
    if extensions:
        ext_expr = " -o ".join(f"-iname {shlex.quote(f'*.{ext}')}" for ext in extensions)
        return f"find {quoted_dirs} -type f \\( {kw_expr} \\) \\( {ext_expr} \\) 2>/dev/null | head -n 50"
    return f"find {quoted_dirs} -type f \\( {kw_expr} \\) 2>/dev/null | head -n 50"


def _build_round2_command(directories: list[str], extensions: list[str]) -> str:
    quoted_dirs = " ".join(shlex.quote(d) for d in directories)
    if not extensions:
        return f"find {quoted_dirs} -type f 2>/dev/null | head -n 200"
    ext_expr = " -o ".join(f"-iname {shlex.quote(f'*.{ext}')}" for ext in extensions)
    return f"find {quoted_dirs} -type f \\( {ext_expr} \\) 2>/dev/null | head -n 200"


def _parse_paths_from_stdout(stdout: str) -> list[str]:
    lines = [(line or "").strip() for line in (stdout or "").splitlines()]
    return _uniq_strings([line for line in lines if line.startswith("/")], max_items=50)


def _render_not_found_message(directories: list[str]) -> str:
    short_dirs = []
    home = str(Path.home())
    for path in directories[:3]:
        short_dirs.append(path.replace(home, "~"))
    scope = "、".join(short_dirs) if short_dirs else "默认目录"
    return f"我在 {scope} 里没有找到明确匹配文件。要不要我扩大范围继续找（比如整个主目录）？"


def _render_found_message(paths: list[str]) -> str:
    shown = paths[:5]
    lines = ["我找到这些可能相关的文件："]
    for idx, item in enumerate(shown, 1):
        lines.append(f"{idx}. {item}")
    if len(paths) > len(shown):
        lines.append(f"另外还有 {len(paths) - len(shown)} 个候选，可继续筛选。")
    return "\n".join(lines)


def _extract_query_terms(user_text: str, keywords: list[str]) -> list[str]:
    text = (user_text or "").strip().lower()
    latin = re.findall(r"[a-z0-9_]{3,}", text)
    cjk = re.findall(r"[\u4e00-\u9fff]{2,}", user_text or "")
    terms = _uniq_strings((keywords or []) + latin + cjk, max_items=20)
    return [item.lower() for item in terms]


def _extract_keywords_from_query(user_text: str) -> list[str]:
    text = (user_text or "").strip()
    if not text:
        return []
    cjk_terms = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    latin_terms = re.findall(r"[a-zA-Z0-9_]{3,}", text.lower())
    merged = _uniq_strings(cjk_terms + latin_terms, max_items=12)
    return merged


def _extract_extensions_from_query(user_text: str) -> list[str]:
    text = (user_text or "").lower()
    found = re.findall(r"\.([a-z0-9]{2,6})", text)
    if found:
        return _uniq_strings(found, max_items=10)
    return []


def _resolve_directories_with_llm(user_text: str, ask_llm_func) -> list[str]:
    home = Path.home()
    try:
        candidates = [str(p) for p in home.iterdir() if p.is_dir()]
    except Exception:
        return []

    candidates = sorted(candidates)[:80]
    if not candidates:
        return []

    prompt = (
        "你是内部目录解析器。"
        "从候选目录中选择与用户请求最相关的目录，最多3个。"
        "只返回JSON，格式: {\"directories\": string[]}。"
        f"\n用户请求: {user_text}\n候选目录:\n" + "\n".join(candidates)
    )
    raw = ask_llm_func([{"role": "user", "content": prompt}])
    data = _extract_json_object(raw) or {}
    return _normalize_directories(data.get("directories", []) or [])


def _safe_read_text(path: Path) -> str:
    try:
        data = path.read_bytes()[:MAX_FILE_READ_BYTES]
    except Exception:
        return ""
    for encoding in ("utf-8", "utf-16", "gb18030", "latin1"):
        try:
            return data.decode(encoding, errors="ignore")
        except Exception:
            continue
    return ""


def _read_docx_text(path: Path) -> str:
    try:
        with zipfile.ZipFile(path, "r") as zf:
            data = zf.read("word/document.xml")
    except Exception:
        return ""
    try:
        root = ElementTree.fromstring(data)
    except Exception:
        return ""
    texts = []
    for node in root.iter():
        if node.tag.endswith("}t") and node.text:
            texts.append(node.text)
    return " ".join(texts)


def _read_pdf_text(path: Path) -> str:
    try:
        proc = subprocess.run(
            ["pdftotext", str(path), "-"],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        out = (proc.stdout or "").strip()
        if out:
            return out[:MAX_FILE_READ_BYTES]
    except Exception:
        pass

    try:
        data = path.read_bytes()[:MAX_FILE_READ_BYTES]
    except Exception:
        return ""
    # Best-effort fallback for PDF streams without external libs.
    # Note: bytes regex cannot use \u escapes.
    chunks = re.findall(rb"[A-Za-z0-9][\x20-\x7e]{8,}", data)
    if not chunks:
        return ""
    return " ".join(chunk.decode("latin1", errors="ignore") for chunk in chunks[:200])


def _extract_file_text(path_str: str) -> str:
    path = Path(path_str)
    suffix = path.suffix.lower().lstrip(".")
    if suffix in TEXT_EXTENSIONS:
        return _safe_read_text(path)
    if suffix == "docx":
        return _read_docx_text(path)
    if suffix == "pdf":
        return _read_pdf_text(path)
    return ""


def _score_candidate(path_str: str, terms: list[str]) -> tuple[float, str]:
    lower_path = path_str.lower()
    name = Path(path_str).name.lower()
    score = 0.0

    for term in terms:
        if not term:
            continue
        if term in name:
            score += 4.0
        elif term in lower_path:
            score += 1.5

    try:
        content = _extract_file_text(path_str).lower()
    except Exception:
        content = ""
    if content:
        matched = 0
        for term in terms[:12]:
            if term and term in content:
                matched += 1
        score += matched * 2.0

    return score, path_str


def _content_rerank_candidates(paths: list[str], terms: list[str]) -> list[str]:
    if not paths:
        return []
    scored = []
    for path in paths[:CONTENT_SCAN_LIMIT]:
        scored.append(_score_candidate(path, terms))
    scored.extend((0.0, item) for item in paths[CONTENT_SCAN_LIMIT:])
    scored.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in scored]


def _planner_prompt(user_text: str) -> str:
    return (
        "你是内部检索规划器。"
        "基于用户请求生成文件检索计划，仅返回 JSON，不要解释。"
        "JSON schema: {\"keywords\": string[], \"directories\": string[], \"extensions\": string[]}。"
        f"用户请求: {user_text}"
    )


def _rerank_prompt(user_text: str, candidates: list[str]) -> str:
    sample = "\n".join(candidates[:30])
    return (
        "你是内部候选筛选器。"
        "从候选路径中选出最相关的最多5项，仅返回 JSON。"
        "JSON schema: {\"selected\": string[]}。"
        f"用户请求: {user_text}\n候选:\n{sample}"
    )


def is_file_search_request(user_text: str) -> bool:
    text = (user_text or "").strip().lower()
    if not text:
        return False
    return any(hint in text for hint in FILE_SEARCH_HINTS)


def run_internal_file_search(user_text: str, ask_llm_func, execute_tool_func, approvals, log_func=None):
    def _log(*parts):
        if log_func:
            try:
                log_func(*parts)
            except Exception:
                pass

    if not is_file_search_request(user_text):
        return {"handled": False}

    _log("内部检索: 启动, user_text =", user_text)
    planner_raw = ask_llm_func([
        {"role": "user", "content": _planner_prompt(user_text)}
    ])
    planner = _extract_json_object(planner_raw) or {}
    _log("内部检索规划:", planner)

    keywords = _uniq_strings(planner.get("keywords", []) or [], max_items=6)
    if not keywords:
        keywords = _extract_keywords_from_query(user_text)
    extensions = _uniq_strings(planner.get("extensions", []) or [], max_items=8)
    if not extensions:
        extensions = _extract_extensions_from_query(user_text)

    directories = _normalize_directories(planner.get("directories", []) or [])
    if not directories:
        directories = _resolve_directories_with_llm(user_text, ask_llm_func)
    if not directories:
        directories = _normalize_directories(DEFAULT_DIRECTORIES)
    query_terms = _extract_query_terms(user_text, keywords)

    commands = []
    if keywords:
        commands.append(_build_round1_command(directories, keywords, extensions))
    commands.append(_build_round2_command(directories, extensions))

    all_candidates = []

    for command in commands:
        _log("内部检索执行命令:", command)
        check = approvals.check_shell_command(command)

        if check.get("decision") == "deny":
            return {
                "handled": True,
                "status": "denied",
                "message": "当前安全策略不允许执行这次检索。",
            }

        if check.get("requires_approval"):
            return {
                "handled": True,
                "status": "approval_required",
                "approval_id": check.get("approval_id"),
                "paths": check.get("restricted_paths", []),
                "message": "本次检索需要访问工作区之外的目录，等待审批。",
            }

        result = execute_tool_func("shell", {"command": command})
        _log("Tool result:", result)
        if not isinstance(result, dict):
            continue

        candidates = _parse_paths_from_stdout(result.get("stdout", ""))
        if candidates:
            all_candidates.extend(candidates)
            all_candidates = _uniq_strings(all_candidates, max_items=80)
            if len(all_candidates) >= 5:
                break

    if not all_candidates:
        _log("内部检索: 未命中候选")
        return {
            "handled": True,
            "status": "not_found",
            "message": _render_not_found_message(directories),
        }

    all_candidates = _content_rerank_candidates(all_candidates, query_terms)
    _log("内部检索: 候选数 =", len(all_candidates))
    selected = all_candidates[:5]
    rerank_raw = ask_llm_func([
        {"role": "user", "content": _rerank_prompt(user_text, all_candidates)}
    ])
    rerank = _extract_json_object(rerank_raw) or {}
    rerank_selected = _uniq_strings(rerank.get("selected", []) or [], max_items=5)
    if rerank_selected:
        whitelist = set(all_candidates)
        selected = [item for item in rerank_selected if item in whitelist] or selected

    return {
        "handled": True,
        "status": "found",
        "message": _render_found_message(selected),
    }
