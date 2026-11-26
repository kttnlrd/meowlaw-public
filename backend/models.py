"""Pydantic models for API request/response validation."""
from __future__ import annotations

from pydantic import BaseModel, Field
from typing import List, Optional


class ActMeta(BaseModel):
    id: str
    name: str
    compilation_no: Optional[str] = None
    compilation_date: Optional[str] = None


class SectionMeta(BaseModel):
    id: str
    title: str
    path: str


class TreeNode(BaseModel):
    id: str
    title: str
    sections: List[SectionMeta] = Field(default_factory=list)
    divisions: List["TreeNode"] = Field(default_factory=list)
    subdivisions: List["TreeNode"] = Field(default_factory=list)


class TreeResponse(BaseModel):
    act: str
    parts: List[TreeNode]


class SearchResult(BaseModel):
    act: str
    section: str
    title: str
    part: Optional[str] = None
    division: Optional[str] = None
    snippet: Optional[str] = None
    exact_match: bool = False


class SearchResponse(BaseModel):
    results: List[SearchResult]
    engine: str


class SectionResponse(BaseModel):
    frontmatter: dict
    body: str


class DefinitionEntry(BaseModel):
    section: str
    anchor: str
    source: str


class DefinitionsResponse(BaseModel):
    act: str
    count: int
    terms: dict[str, DefinitionEntry]


class CommentaryEntry(BaseModel):
    publication: str
    chapter_number: Optional[str] = None
    chapter_title: Optional[str] = None
    heading_title: str
    paragraph_number: Optional[str] = None
    content_blocks: List[dict] = Field(default_factory=list)
    sub_headings: List[dict] = Field(default_factory=list)


class CommentaryResponse(BaseModel):
    act: str
    section: str
    count: int
    commentary: List[CommentaryEntry]


class CaseEntry(BaseModel):
    citation: str
    title: str
    short_name: str
    category: str
    court: str
    year: int
    date: str
    source_url: str


class CasesResponse(BaseModel):
    act: str
    section: str
    count: int
    cases: List[dict]


class RulingEntry(BaseModel):
    citation: str
    title: str
    type: str
    year: int
    source: str
    preview: str


class RulingsResponse(BaseModel):
    act: str
    section: str
    count: int
    rulings: List[dict]


class HealthResponse(BaseModel):
    status: str = "ok"
