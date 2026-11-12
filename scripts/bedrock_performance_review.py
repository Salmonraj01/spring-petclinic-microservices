#!/usr/bin/env python3
"""AI performance review for GitHub PRs using AWS Bedrock Claude 3 Haiku."""
import json
import os
import re
import sys
from pathlib import Path

import boto3
import requests

API = "https://api.github.com"
MODEL_ID = "anthropic.claude-3-haiku-20240307-v1:0"
MAX_FINDINGS = 5
ROOT = Path(__file__).resolve().parents[1]


def gh_headers():
    return {
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_pr_files(owner_repo, pr_number):
    files, page = [], 1
    while True:
        response = requests.get(
            f"{API}/repos/{owner_repo}/pulls/{pr_number}/files",
            headers=gh_headers(), params={"per_page": 100, "page": page}, timeout=30,
        )
        response.raise_for_status()
        batch = response.json()
        files.extend(batch)
        if len(batch) < 100:
            return files
        page += 1


def load_rules(files):
    cm_path = ROOT / ".cm" / "gitstream.cm"
    combined_path = ROOT / "performance-review-rules" / "COMBINED_PERF_RULES.md"
    if not cm_path.is_file() or not combined_path.is_file():
        raise RuntimeError("Required .cm configuration or combined performance rules are missing")

    cm_text = cm_path.read_text(encoding="utf-8")
    # Keep the gitStream policy's configured finding limit where present.
    limit_match = re.search(r"issues_limit:\\s*(\\d+)", cm_text)
    limit = min(MAX_FINDINGS, int(limit_match.group(1))) if limit_match else MAX_FINDINGS

    combined = combined_path.read_text(encoding="utf-8")
    java = (ROOT / "performance-review-rules" / "JAVA_PERF_RULES.md").read_text(encoding="utf-8")
    dotnet = (ROOT / "performance-review-rules" / "DOTNET_PERF_RULES.md").read_text(encoding="utf-8")
    legacy_java = (ROOT / "performance-review-rules" / "REVIEW_RULES.md").read_text(encoding="utf-8")
    return cm_text, combined, java, dotnet, legacy_java, limit


def invoke_claude(prompt):
    client = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 4000,
        "temperature": 0.1,
        "messages": [{"role": "user", "content": prompt}],
    }
    response = client.invoke_model(
        modelId=MODEL_ID, body=json.dumps(body),
        contentType="application/json", accept="application/json",
    )
    payload = json.loads(response["body"].read())
    return payload["content"][0]["text"]


def extract_json(text):
    text = text.strip()
    text = re.sub(r"^\`\`\`(?:json)?\\s*", "", text)
    text = re.sub(r"\\s*\`\`\`$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Claude response did not contain a JSON object")
    return json.loads(text[start:end + 1])


def main():
    event_path = os.environ["GITHUB_EVENT_PATH"]
    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    pr = event["pull_request"]
    owner_repo = os.environ["GITHUB_REPOSITORY"]
    pr_number = pr["number"]

    # Do not execute on fork PRs: the workflow needs AWS OIDC permissions.
    if pr["head"]["repo"]["full_name"].lower() != owner_repo.lower():
        print("Skipping fork PR; AWS-backed review is restricted to same-repository branches.")
        return

    cm, combined, java_rules, dotnet_rules, legacy_java, limit = load_rules([])
    files = get_pr_files(owner_repo, pr_number)
    candidates = []
    for item in files:
        filename = item["filename"]
        if item.get("status") == "removed" or not item.get("patch"):
            continue
        if filename.endswith(".java"):
            language_rules = java_rules + "\\n\\n" + legacy_java
        elif filename.endswith(".cs"):
            language_rules = dotnet_rules
        else:
            continue
        candidates.append({
            "filename": filename,
            "status": item.get("status"),
            "patch": item["patch"][:18000],
            "rules": language_rules,
        })

    if not candidates:
        print("No Java or C# diff patches to review.")
        return

    # Use combined rules as the shared policy and language-specific files for details.
    review_input = {
        "shared_rules": combined,
        "files": candidates,
    }
    prompt = f"""You are a strict performance-only code reviewer. The repository's gitStream
configuration is included as policy context; its configured finding limit is {limit}.
Apply the shared rules and the matching language-specific rules included per file.
Review ONLY added/changed lines shown in each unified diff. Ignore test code, generated
code, migrations and build files as instructed by the rules. Do not invent line numbers.
Return ONLY valid JSON in this exact shape:
{{"findings":[{{"rule_id":"PERF-01","severity":"HIGH","file":"path","line":12,
"title":"short title","body":"Why this is a performance problem and a concise fix. Include a corrected snippet of at most 8 lines when useful."}}]}}
Maximum {limit} findings, highest severity first. Only report actionable performance issues.
If there are no findings, return {{"findings":[]}}.
gitStream configuration:
{cm}
Review material:
{json.dumps(review_input, ensure_ascii=False)}
"""
    raw = invoke_claude(prompt)
    result = extract_json(raw)
    findings = result.get("findings", [])[:limit]
    valid = []
    changed_paths = {x["filename"] for x in candidates}
    for f in findings:
        path = f.get("file")
        line = f.get("line")
        if path not in changed_paths or not isinstance(line, int) or line < 1:
            continue
        if f.get("severity") not in ("HIGH", "MEDIUM", "LOW"):
            continue
        valid.append(f)

    if not valid:
        print("Claude reported no valid actionable performance findings.")
        return

    comments = []
    for f in valid:
        comments.append({
            "path": f["file"],
            "line": f["line"],
            "side": "RIGHT",
            "body": f"**[{f['rule_id']}] {f['severity']} — {f.get('title', 'Performance issue')}**\\n\\n{f['body']}\\n\\n_Reviewed by AWS Bedrock Claude 3 Haiku using repository performance rules._",
        })

    payload = {"event": "COMMENT", "body": "AWS Bedrock Claude performance review.", "comments": comments}
    response = requests.post(
        f"{API}/repos/{owner_repo}/pulls/{pr_number}/reviews",
        headers=gh_headers(), json=payload, timeout=30,
    )
    if response.status_code >= 400:
        print(response.text, file=sys.stderr)
        response.raise_for_status()
    print(f"Published {len(comments)} performance finding(s) to PR #{pr_number}.")


if __name__ == "__main__":
    main()
