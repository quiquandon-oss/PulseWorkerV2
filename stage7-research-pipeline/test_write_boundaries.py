"""
Boundary checks for the human-controlled Stage 7 workflow (static, no network):

  * run_stage7.py (the only Stage 7 Python writer) may only write Stage 7 tables, and may never INSERT/DELETE a
    response (responses are written solely by a human through the Worker),
  * no Stage 7 code path can call an external AI/LLM service (the human copies the prompt to one manually),
  * no write ever touches V1/V2 prediction, history or price tables.

Run with: python3 -m pytest stage7-research-pipeline/ -v
"""
import ast
import os
import re

HERE = os.path.dirname(__file__)
REPO = os.path.abspath(os.path.join(HERE, ".."))

STAGE7_PY = [
    os.path.join(REPO, "stage7-research-pipeline", "run_stage7.py"),
    os.path.join(REPO, "research", "stage7_evidence_sufficiency.py"),
    os.path.join(REPO, "research", "stage7_sentiment_recalculation.py"),
    os.path.join(REPO, "research", "stage7_github_publisher.py"),
]


def _strings_in(node):
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _targets_in(texts):
    out = []
    for text in texts:
        for m in re.finditer(r"\b(INSERT INTO|DELETE FROM)\s+(\w+)|\bUPDATE\s+(\w+)\s+SET", text):
            out.append(((m.group(1) or "UPDATE"), m.group(2) or m.group(3)))
    return out


def _remote_d1_write_targets(path):
    """Writes sent to the REAL database: SQL passed to d1_api_query()/run_d1(). (run_stage7.py also INSERTs into a
    throwaway in-memory sqlite mirror of history/btc_data/...; that never leaves the process and is excluded.)"""
    tree = ast.parse(open(path, encoding="utf-8").read())
    texts = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", getattr(node.func, "attr", None))
            if name in ("d1_api_query", "run_d1") and node.args:
                texts.extend(_strings_in(node.args[0]))
    return _targets_in(texts)


def _any_write_targets(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    return _targets_in(_strings_in(tree))


def test_run_stage7_writes_only_stage7_tables_to_the_real_database():
    targets = _remote_d1_write_targets(STAGE7_PY[0])
    assert targets, "expected run_stage7.py to send write statements to D1"
    assert {t for _, t in targets} <= {"stage7_research_requests", "stage7_research_candidates", "stage7_event_sentiment"}


def test_run_stage7_never_inserts_deletes_or_updates_a_response():
    assert not [t for _, t in _remote_d1_write_targets(STAGE7_PY[0]) if t == "stage7_research_responses"]


def test_the_pure_stage7_modules_contain_no_write_statement_at_all():
    for path in STAGE7_PY[1:]:
        assert _any_write_targets(path) == [], path


def test_stage7_python_never_imports_an_ai_or_http_client_library():
    banned = {"anthropic", "openai", "google", "requests", "httpx", "aiohttp", "cohere", "mistralai"}
    for path in STAGE7_PY:
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            assert not (set(names) & banned), (path, names)


AI_HOSTS = ("api.anthropic.com", "api.openai.com", "generativelanguage.googleapis.com", "api.x.ai", "chatgpt.com/backend")


def test_no_ai_api_host_appears_in_stage7_python_or_in_the_worker_stage7_region():
    for path in STAGE7_PY:
        text = open(path, encoding="utf-8").read()
        for host in AI_HOSTS:
            assert host not in text, (path, host)
    worker = open(os.path.join(REPO, "worker.js"), encoding="utf-8").read()
    start = worker.index("// ---- Stage 7 deployment gate, schema readiness, and safe error mapping ----")
    end = worker.index("// ---- EXPERIMENT REGISTRY ----")
    region = worker[start:end]
    for host in AI_HOSTS:
        assert host not in region, host
    # The Stage 7 Worker region performs no outbound fetch at all.
    assert not re.search(r"\bfetch\(", region)


def test_worker_stage7_frontend_only_opens_external_ai_sites_as_plain_links_never_programmatic_calls():
    worker = open(os.path.join(REPO, "worker.js"), encoding="utf-8").read()
    start = worker.index("function stage7CopyText")
    end = worker.index("function stage7FormHtml")
    # Clipboard helper: no network call.
    assert not re.search(r"\bfetch\(", worker[start:end])
