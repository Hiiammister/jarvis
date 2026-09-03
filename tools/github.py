"""tools/github.py — back up THIS Bella project to the user's GitHub account."""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

from tools.base import PermissionLevel, Tool
from tools.registry import register

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _git(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=timeout,
    )


def _gh_authenticated() -> bool:
    try:
        return subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True, timeout=15,
        ).returncode == 0
    except FileNotFoundError:
        return False


def run_github(action: str, message: str = None, private: bool = None) -> dict:
    if action == "status":
        status = _git(["status", "--porcelain"])
        remote = _git(["remote", "get-url", "origin"])
        return {
            "success": True,
            "has_uncommitted_changes": bool(status.stdout.strip()),
            "changed_files": len(status.stdout.strip().splitlines()) if status.stdout.strip() else 0,
            "remote_url": remote.stdout.strip() if remote.returncode == 0 else None,
        }

    if action == "upload":
        try:
            subprocess.run(["gh", "--version"], capture_output=True, timeout=5)
        except FileNotFoundError:
            return {"success": False, "error": "GitHub CLI (`gh`) is not installed."}
        if not _gh_authenticated():
            return {"success": False, "error": "Not logged into GitHub — run `gh auth login` first (needs a browser)."}

        status = _git(["status", "--porcelain"])
        had_changes = bool(status.stdout.strip())
        if had_changes:
            add = _git(["add", "-A"])
            if add.returncode != 0:
                return {"success": False, "error": add.stderr or "git add failed"}
            commit_msg = message or f"Update via Bella — {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            commit = _git(["commit", "-m", commit_msg])
            if commit.returncode != 0:
                return {"success": False, "error": commit.stderr or commit.stdout or "git commit failed"}

        remote = _git(["remote", "get-url", "origin"])
        has_remote = remote.returncode == 0

        if not has_remote:
            visibility = "--public" if private is False else "--private"
            create = subprocess.run(
                ["gh", "repo", "create", PROJECT_ROOT.name, visibility,
                 "--source=.", "--remote=origin", "--push"],
                cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60,
            )
            if create.returncode != 0:
                return {"success": False, "error": create.stderr or create.stdout or "gh repo create failed"}
            url = _git(["remote", "get-url", "origin"]).stdout.strip()
            return {"success": True, "action": "created_and_pushed", "repo_url": url, "committed": had_changes}

        push = _git(["push", "origin", "HEAD"], timeout=60)
        if push.returncode != 0:
            return {"success": False, "error": push.stderr or push.stdout or "git push failed"}
        return {"success": True, "action": "pushed", "repo_url": remote.stdout.strip(), "committed": had_changes}

    return {"error": f"Unknown github action: {action}"}


class GithubTool(Tool):
    name = "github"
    description = (
        "Back up THIS jarvis project (this exact codebase, not any other folder — "
        "there is no `path` parameter) to the user's own GitHub account. Use this for "
        "'upload to GitHub', 'push to GitHub', 'back this up', 'sync to GitHub', or similar.\n"
        "Actions:\n"
        "  - status : report whether there are uncommitted changes and what the "
        "remote repo URL is (if any) — use this to check before/without pushing.\n"
        "  - upload : commit any pending changes (auto-generated message unless "
        "`message` is given) and push. If no GitHub repo exists yet for this project, "
        "creates one on the user's account first (name defaults to the project folder "
        "name; pass `private=false` for a public repo, defaults to private) and pushes "
        "to it. Requires `gh auth login` to have already been run once by the user — if "
        "not authenticated, this fails with a clear error; tell the user to run that, "
        "don't try to work around it."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "upload"],
                "description": "The GitHub operation.",
            },
            "message": {"type": "string", "description": "Commit message (action=upload). Optional — a timestamped default is used if omitted."},
            "private": {"type": "boolean", "description": "Repo visibility if creating one for the first time (action=upload). Defaults to true (private)."},
        },
        "required": ["action"],
    }
    permission = PermissionLevel.WRITE

    def assess(self, arguments: dict) -> PermissionLevel:
        return PermissionLevel.READ_ONLY if arguments.get("action") == "status" else PermissionLevel.WRITE

    def execute(self, action: str, message: str = None, private: bool = None) -> dict:
        return run_github(action, message, private)


register(GithubTool())
