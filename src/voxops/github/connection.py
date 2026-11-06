import json
from urllib.parse import urlsplit

import httpx

from voxops.core.errors import AppError
from voxops.domain.artifact_models import RepositoryTarget


# Connects to the configured GitHub tool server.
# Discovers its available tools and uses them for repository operations.
class GitHubConnection:
    # Sets up the connection to the GitHub tool server.
    # Uses host settings and an optional supplied HTTP client.
    def __init__(self, settings, client: httpx.AsyncClient | None = None):
        self.url = getattr(settings, "github_mcp_url", None)
        secret = getattr(settings, "github_mcp_token", None)
        self.token = secret.get_secret_value() if secret else None
        self.client = client
        self.headers = {"Accept": "application/json, text/event-stream"}
        self.tools: dict[str, dict] = {}
        self.identity: str | None = None
        self.sequence = 0

    # Sends a request to the GitHub tool server.
    # Matches the response to the request and handles JSON or event-stream replies.
    async def rpc(self, method: str, params: dict, notification: bool = False) -> dict:
        if not self.url:
            raise AppError(
                "github_mcp_unavailable",
                "Configure VOXOPS_GITHUB_MCP_URL and authenticate the GitHub MCP server.",
                409,
            )
        parsed = urlsplit(self.url)
        if (
            parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.scheme not in {"http", "https"}
            or (
                parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            )
        ):
            raise AppError(
                "github_mcp_unavailable",
                "GitHub MCP requires HTTPS or a loopback HTTP endpoint.",
                409,
            )
        self.sequence += 1
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            payload["id"] = self.sequence
        headers = dict(self.headers)
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        client = self.client or httpx.AsyncClient(
            timeout=30, follow_redirects=False, trust_env=False
        )
        try:
            async with client.stream("POST", self.url, json=payload, headers=headers) as response:
                response.raise_for_status()
                if session := response.headers.get("Mcp-Session-Id"):
                    self.headers["Mcp-Session-Id"] = session
                if notification:
                    return {}
                if "text/event-stream" in response.headers.get("content-type", ""):
                    size = 0
                    event = []
                    async for line in response.aiter_lines():
                        size += len(line)
                        if size > 2097152:
                            raise ValueError("Response too large")
                        if line.startswith("data:"):
                            event.append(line[5:].lstrip())
                        elif not line and event:
                            candidate = json.loads("\n".join(event))
                            event = []
                            if candidate.get("id") == payload["id"]:
                                result = candidate
                                break
                    else:
                        raise ValueError("Missing RPC response")
                else:
                    body = await response.aread()
                    if len(body) > 2097152:
                        raise ValueError("Response too large")
                    result = json.loads(body)
                if result.get("id") != payload["id"] or "error" in result:
                    raise ValueError("RPC failure")
                return result["result"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise AppError(
                "github_mcp_failed",
                "GitHub MCP request failed; check authentication and enabled tools.",
                409,
            ) from None
        finally:
            if self.client is None:
                await client.aclose()

    # Calls a tool offered by the GitHub server.
    # Checks its declared arguments and reads the returned structured data or JSON.
    async def call(self, name: str, arguments: dict) -> dict:
        schema = self.tools.get(name, {}).get("inputSchema", {})
        properties = schema.get("properties", {})
        if (
            name not in self.tools
            or set(arguments) - set(properties)
            or set(schema.get("required", [])) - set(arguments)
        ):
            raise AppError(
                "github_mcp_capability", "GitHub MCP lacks a compatible required tool schema.", 409
            )
        result = await self.rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise AppError("github_mcp_failed", "GitHub MCP tool rejected the request.", 409)
        if isinstance(result.get("structuredContent"), dict):
            return result["structuredContent"]
        try:
            text = "\n".join(
                item["text"] for item in result.get("content", []) if item.get("type") == "text"
            )
            value = json.loads(text)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, KeyError, TypeError):
            raise AppError(
                "github_mcp_metadata", "GitHub MCP returned unsupported metadata.", 409
            ) from None

    # Checks whether the GitHub connection is ready for repository work.
    # Discovers available tools and retrieves the authenticated account.
    async def probe(self, require_create: bool = False) -> str:
        if not self.tools:
            initialized = await self.rpc(
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "voxops", "version": "0.2.0"},
                },
            )
            protocol = initialized.get("protocolVersion")
            if protocol not in {"2025-03-26", "2025-06-18", "2025-11-25"}:
                raise AppError(
                    "github_mcp_capability", "GitHub MCP negotiated an unsupported protocol.", 409
                )
            self.headers["MCP-Protocol-Version"] = protocol
            await self.rpc("notifications/initialized", {}, notification=True)
            params = {}
            for _ in range(20):
                listing = await self.rpc("tools/list", params)
                for tool in listing.get("tools", []):
                    self.tools[tool["name"]] = tool
                if not listing.get("nextCursor"):
                    break
                params = {"cursor": listing["nextCursor"]}
            else:
                raise AppError(
                    "github_mcp_capability", "GitHub MCP tool inventory exceeds the limit.", 409
                )
        needed = {"get_me", "search_repositories"}
        if require_create:
            needed.add("create_repository")
        if not needed <= self.tools.keys():
            raise AppError(
                "github_mcp_capability",
                "Enable get_me, search_repositories and repository creation when required.",
                409,
            )
        user = await self.call("get_me", {})
        login = user.get("login")
        if not isinstance(login, str) or not login:
            raise AppError(
                "github_mcp_metadata", "GitHub MCP did not return an authenticated identity.", 409
            )
        self.identity = login
        return login

    # Finds a specific GitHub repository.
    # Checks its full owner and name along with the account's permissions.
    async def lookup(
        self, target: RepositoryTarget, creation_id: str | None = None
    ) -> RepositoryTarget | None:
        await self.probe()
        arguments = {"query": f"repo:{target.owner}/{target.name}"}
        schema = self.tools["search_repositories"].get("inputSchema", {}).get("properties", {})
        if "minimal_output" in schema:
            arguments["minimal_output"] = False
        result = await self.call("search_repositories", arguments)
        if result.get("incomplete_results"):
            raise AppError(
                "github_mcp_metadata", "GitHub search returned incomplete repository evidence.", 409
            )
        repositories = result.get("items")
        if not isinstance(repositories, list):
            raise AppError(
                "github_mcp_metadata", "GitHub MCP did not return repository search evidence.", 409
            )
        matches = [
            repo
            for repo in repositories
            if repo.get("full_name", "").lower() == f"{target.owner}/{target.name}".lower()
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise AppError("github_mcp_metadata", "GitHub repository identity was ambiguous.", 409)
        repo = matches[0]
        if creation_id and repo.get("description") != f"VoxOps publication {creation_id}":
            raise AppError(
                "git_collision", "Repository creation ownership could not be reconciled.", 409
            )
        owner_data = repo.get("owner")
        owner = owner_data.get("login") if isinstance(owner_data, dict) else None
        owner = owner or repo["full_name"].partition("/")[0]
        private, permissions = repo.get("private"), repo.get("permissions", {})
        writable = permissions.get("push") if isinstance(permissions, dict) else None
        if writable is None:
            writable = await self.collaborator_permission(target)
        clone_url = f"https://github.com/{target.owner}/{target.name}.git"
        if (
            not isinstance(private, bool)
            or not repo.get("id")
            or owner is None
            or owner.lower() != target.owner.lower()
            or writable is not True
            or repo.get("clone_url", clone_url) != clone_url
        ):
            raise AppError(
                "github_mcp_metadata",
                "MCP must provide ID, owner, visibility and authoritative push permission.",
                409,
            )
        if target.repository_id and target.repository_id != str(repo["id"]):
            raise AppError("remote_mismatch", "GitHub repository identity changed.", 409)
        return RepositoryTarget(
            owner=target.owner,
            name=target.name,
            repository_id=str(repo["id"]),
            private=private,
            clone_url=clone_url,
            can_push=True,
        )

    # Checks the account's access to a GitHub repository.
    # Uses the available collaborator tool to find its permission level.
    async def collaborator_permission(self, target: RepositoryTarget) -> bool:
        if "list_repository_collaborators" not in self.tools:
            raise AppError(
                "github_mcp_capability",
                "Enable list_repository_collaborators to verify GitHub push permission.",
                409,
            )
        properties = (
            self.tools["list_repository_collaborators"].get("inputSchema", {}).get("properties", {})
        )
        for page in range(1, 21):
            arguments = {"owner": target.owner, "repo": target.name}
            if "perPage" in properties:
                arguments["perPage"] = 100
            if "page" in properties:
                arguments["page"] = page
            elif page != 1:
                break
            result = await self.call("list_repository_collaborators", arguments)
            items = result.get("items")
            if not isinstance(items, list):
                raise AppError(
                    "github_mcp_metadata", "GitHub collaborator evidence is unavailable.", 409
                )
            for collaborator in items:
                if (
                    isinstance(collaborator, dict)
                    and isinstance(collaborator.get("login"), str)
                    and collaborator["login"].lower() == self.identity.lower()
                ):
                    role = collaborator.get("role_name")
                    return isinstance(role, str) and role in {"admin", "maintain", "write"}
            if not result.get("nextPage"):
                return False
        raise AppError(
            "github_mcp_metadata", "GitHub collaborator evidence exceeds the page limit.", 409
        )

    # Creates a private GitHub repository.
    # Requests an empty repository without adding an initial commit.
    async def create(self, target: RepositoryTarget, creation_id: str | None = None) -> None:
        identity = await self.probe(require_create=True)
        if not target.private:
            raise AppError("private_repository_required", "New repositories must be private.", 409)
        arguments = {"name": target.name, "private": True, "autoInit": False}
        if creation_id:
            arguments["description"] = f"VoxOps publication {creation_id}"
        if identity.lower() != target.owner.lower():
            arguments["organization"] = target.owner
        await self.call("create_repository", arguments)
