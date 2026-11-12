# Local AWS Bedrock Performance Reviewer

This reviewer runs from the repository codebase. It uses the existing gitStream
configuration and all four existing Markdown performance-rule files without
modifying their contents.

## 1. Configure AWS credentials locally

Copy `.env.example` to `.env` and fill in your temporary AWS credentials.
Never commit `.env`. The repository ignores it.

PowerShell (current terminal only):

```powershell
$env:AWS_ACCESS_KEY_ID = "YOUR_ACCESS_KEY"
$env:AWS_SECRET_ACCESS_KEY = "YOUR_SECRET_KEY"
$env:AWS_SESSION_TOKEN = "YOUR_SESSION_TOKEN"
$env:AWS_REGION = "us-east-1"
```

Do not paste credentials into source code, command history, or chat. Temporary
session credentials expire; replace them when AWS reports an expired token.

## 2. Install dependencies

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r scripts/bedrock-review-requirements.txt
```

## 3. Review local changes

Review uncommitted changes:

```powershell
python scripts/bedrock_performance_review.py
```

Review the current branch against main:

```powershell
git fetch origin
python scripts/bedrock_performance_review.py --base-ref origin/main
```

Save results to a JSON file:

```powershell
python scripts/bedrock_performance_review.py --base-ref origin/main --output bedrock-review.json
```

Or review a saved unified diff:

```powershell
python scripts/bedrock_performance_review.py --diff-file review.patch
```

## 4. AWS requirements

The active AWS identity needs permission for `bedrock:InvokeModel` on
`anthropic.claude-3-haiku-20240307-v1:0` in `us-east-1`, and model access must
be enabled in that account/region.

## Scope

- Java changes use `JAVA_PERF_RULES.md` and `REVIEW_RULES.md`.
- C# changes use `DOTNET_PERF_RULES.md`.
- Shared scope and output policy come from `COMBINED_PERF_RULES.md`.
- `.cm/gitstream.cm` supplies the configured finding limit.
- This local phase prints structured JSON; GitHub PR comments and Actions
  automation will be added after local validation.
