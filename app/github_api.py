from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote

import httpx


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
OUTPUT_ROOTS = {"جاهز", "مكرر", "مراجعة", "metadata"}


class GitHubAPI:
    def __init__(
        self,
        repo: str,
        branch: str,
        token: str = "",
        timeout: float = 30,
    ):
        self.repo = repo
        self.branch = branch
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = httpx.Client(
            base_url=f"https://api.github.com/repos/{repo}",
            headers=headers,
            timeout=timeout,
        )

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.client.get(path, params=params or {})
        response.raise_for_status()
        return response.json()

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post(path, json=body)
        response.raise_for_status()
        return response.json()

    def _patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self.client.patch(path, json=body)
        response.raise_for_status()
        return response.json()

    def _branch_commit(self) -> tuple[str, str]:
        ref = self._get(f"/git/ref/heads/{quote(self.branch, safe='')}")
        commit_sha = ref["object"]["sha"]
        commit = self._get(f"/git/commits/{commit_sha}")
        return commit_sha, commit["tree"]["sha"]

    def recursive_tree(self) -> list[dict[str, Any]]:
        _, tree_sha = self._branch_commit()
        data = self._get(f"/git/trees/{tree_sha}", {"recursive": "1"})
        if data.get("truncated"):
            raise RuntimeError(
                "GitHub returned a truncated poster tree. Stop rather than silently skip folders."
            )
        return list(data.get("tree") or [])

    def list_film_folders(self) -> list[dict[str, str]]:
        tree = self.recursive_tree()
        grouped: dict[str, list[str]] = {}
        for item in tree:
            if item.get("type") != "blob":
                continue
            path = str(item.get("path") or "")
            parts = path.split("/")
            if len(parts) != 2 or parts[0] in OUTPUT_ROOTS:
                continue
            filename = parts[1]
            suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if suffix not in IMAGE_EXTENSIONS:
                continue
            grouped.setdefault(parts[0], []).append(path)

        folders = []
        for folder, paths in grouped.items():
            first_image = sorted(paths, key=lambda path: path.rsplit("/", 1)[-1].casefold())[0]
            folders.append({"folder": folder, "image_path": first_image})
        return sorted(folders, key=lambda item: item["folder"].casefold())

    def raw_url(self, path: str) -> str:
        encoded = "/".join(quote(part, safe="") for part in path.split("/"))
        return f"https://raw.githubusercontent.com/{self.repo}/{self.branch}/{encoded}"

    def get_json_file(self, path: str) -> dict[str, Any] | None:
        response = self.client.get(
            f"/contents/{quote(path, safe='/')}",
            params={"ref": self.branch},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json()
        encoded = data.get("content") or ""
        if not encoded or data.get("encoding") != "base64":
            # Large files may not include content in the Contents API response.
            blob = self._get(f"/git/blobs/{data['sha']}")
            encoded = blob.get("content") or ""
        content = base64.b64decode(encoded).decode("utf-8")
        import json
        return json.loads(content)

    def commit_files(
        self,
        files: dict[str, str],
        deletes: list[str] | None = None,
        message: str = "Add KafiaNet movie metadata batch",
    ) -> str:
        if not files and not deletes:
            raise ValueError("No files or deletions to commit")

        expected_head, base_tree = self._branch_commit()
        tree_items: list[dict[str, Any]] = []

        for path, content in sorted(files.items()):
            blob = self._post(
                "/git/blobs",
                {"content": content, "encoding": "utf-8"},
            )
            tree_items.append(
                {"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]}
            )

        for path in sorted(set(deletes or [])):
            tree_items.append(
                {"path": path, "mode": "100644", "type": "blob", "sha": None}
            )

        tree = self._post(
            "/git/trees",
            {"base_tree": base_tree, "tree": tree_items},
        )
        commit = self._post(
            "/git/commits",
            {
                "message": message,
                "tree": tree["sha"],
                "parents": [expected_head],
            },
        )
        updated = self._patch(
            f"/git/refs/heads/{quote(self.branch, safe='')}",
            {"sha": commit["sha"], "force": False},
        )
        return updated["object"]["sha"]

    def close(self) -> None:
        self.client.close()
