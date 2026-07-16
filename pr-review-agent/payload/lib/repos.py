import logging
import os
import subprocess

log = logging.getLogger("repos")


def repo_dir(org: str, repo: str, repos_base: str) -> str:
    return os.path.join(repos_base, org, repo)


def clone_or_fetch(org: str, repo: str, repos_base: str) -> None:
    dest = os.path.join(repos_base, org, repo)
    if not os.path.isdir(os.path.join(dest, ".git")):
        log.info("Cloning %s/%s...", org, repo)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        subprocess.run(
            ["git", "clone", "--depth=1",
             f"https://github.com/{org}/{repo}.git", dest],
            check=True, capture_output=True,
        )
    else:
        log.info("Fetching %s/%s...", org, repo)
        subprocess.run(
            ["git", "-C", dest, "fetch", "--prune", "--depth=1", "origin"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", dest, "checkout", "FETCH_HEAD"],
            check=True, capture_output=True,
        )
