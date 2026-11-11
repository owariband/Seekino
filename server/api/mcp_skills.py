"""Publish the installed editing guide and its script through MCP Resources and SEP-2640."""

from hashlib import sha256
from pathlib import Path

import yaml
from mcp.server.extension import Extension, MethodBinding, ResourceBinding
from mcp.server.mcpserver.resources import TextResource
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, PaginatedRequestParams, RequestParams


class GetSkillParams(RequestParams):
    uri: str


class EditingSkill(Extension):
    identifier = "io.modelcontextprotocol/skills"

    def __init__(self, directory: Path):
        name = directory.name
        self._resources = []
        manifest = []
        for filename, mime in (("SKILL.md", "text/markdown"), ("scripts/split_frame.py", "text/x-python")):
            raw = (directory / filename).read_bytes()
            uri = f"skill://{name}/{filename}"
            resource = TextResource(uri=uri, name=filename, mime_type=mime, text=raw.decode("utf-8"))
            self._resources.append(ResourceBinding(resource))
            manifest.append({"uri": uri, "size": len(raw), "digest": "sha256:" + sha256(raw).hexdigest()})
        guide = self._resources[0].resource.text
        frontmatter = yaml.safe_load(guide.split("---\n", 2)[1])
        if frontmatter["name"] != name:
            raise ValueError("Packaged skill name does not match its directory")
        self.entry = {"uri": manifest[0]["uri"], "frontmatter": frontmatter, "resources": manifest}

    def resources(self):
        return self._resources

    def methods(self):
        versions = frozenset({"2026-07-28"})
        return (MethodBinding("skills/list", PaginatedRequestParams, self.list_skills, versions),
                MethodBinding("skills/get", GetSkillParams, self.get_skill, versions))

    async def list_skills(self, ctx, params):
        if params.cursor:
            raise MCPError(INVALID_PARAMS, "Unknown skill cursor")
        return {"resultType": "complete", "skills": [self.entry]}

    async def get_skill(self, ctx, params):
        if params.uri != self.entry["uri"]:
            raise MCPError(INVALID_PARAMS, "Unknown skill URI")
        return {"resultType": "complete", "skill": self.entry}


def editing_skill() -> EditingSkill:
    return EditingSkill(Path(__file__).resolve().parents[1] / "skills" / "seekino-editing-materials")
