"""File discovery tools: list top folders, list folder contents, file metadata, search."""

from __future__ import annotations

from typing import Any

from ..client import KiteworksAPIError, KiteworksClient
from ..formatting import make_error_response, make_kv_response, make_list_response
from ..schemas import (
    GetFileMetadataInput,
    ListFolderContentsInput,
    ListTopFoldersInput,
    SearchFilesInput,
    ToolResponse,
)

_FOLDER_COLS = ["id", "name", "type", "totalFilesCount", "totalFoldersCount", "modified"]
_FILE_COLS = ["id", "name", "type", "size", "fingerprint", "modified"]
_MIXED_COLS = ["id", "name", "type", "size", "modified"]


def _slim_folder(f: dict[str, Any]) -> dict[str, Any]:
    return {c: f.get(c) for c in _FOLDER_COLS}


def _slim_file(f: dict[str, Any]) -> dict[str, Any]:
    return {c: f.get(c) for c in _FILE_COLS}


def _slim_mixed(entry: dict[str, Any]) -> dict[str, Any]:
    return {c: entry.get(c) for c in _MIXED_COLS}


async def list_top_folders(client: KiteworksClient, params: ListTopFoldersInput) -> ToolResponse:
    try:
        all_folders = await client.list_top_folders()
    except KiteworksAPIError as e:
        return make_error_response(
            code=f"KITEWORKS_{e.status}",
            message=str(e),
            details={"body": e.body},
            fmt=params.response_format,
        )
    sliced = all_folders[params.offset : params.offset + params.limit]
    return make_list_response(
        items=[_slim_folder(f) for f in sliced],
        offset=params.offset,
        limit=params.limit,
        total=len(all_folders),
        fmt=params.response_format,
        title="Top-level Kiteworks folders",
        columns=_FOLDER_COLS,
    )


async def list_folder_contents(
    client: KiteworksClient, params: ListFolderContentsInput
) -> ToolResponse:
    try:
        children = await client.list_folder_children(
            params.folder_id,
            name_filter=params.name_contains,
            extensions=params.extensions,
        )
    except KiteworksAPIError as e:
        return make_error_response(
            code=f"KITEWORKS_{e.status}",
            message=str(e),
            details={"body": e.body, "folder_id": params.folder_id},
            fmt=params.response_format,
        )
    sliced = children[params.offset : params.offset + params.limit]
    return make_list_response(
        items=[_slim_mixed(c) for c in sliced],
        offset=params.offset,
        limit=params.limit,
        total=len(children),
        fmt=params.response_format,
        title=f"Contents of folder {params.folder_id}",
        columns=_MIXED_COLS,
    )


async def get_file_metadata(
    client: KiteworksClient, params: GetFileMetadataInput
) -> ToolResponse:
    try:
        f = await client.get_file_metadata(params.file_id)
    except KiteworksAPIError as e:
        code = "FILE_NOT_FOUND" if e.status == 404 else f"KITEWORKS_{e.status}"
        return make_error_response(
            code=code,
            message=str(e),
            details={"body": e.body, "file_id": params.file_id},
            fmt=params.response_format,
        )
    summary = {
        "id": f.get("id"),
        "name": f.get("name"),
        "parentId": f.get("parentId"),
        "size_bytes": f.get("size"),
        "mime": f.get("mime"),
        "fingerprint": f.get("fingerprint"),
        "created": f.get("created"),
        "modified": f.get("modified"),
        "expire": f.get("expire"),
        "deleted": f.get("deleted"),
        "av_status": f.get("avStatus"),
    }
    return make_kv_response(
        data=summary,
        title=f"File {f.get('name', params.file_id)}",
        fmt=params.response_format,
    )


async def search_files(client: KiteworksClient, params: SearchFilesInput) -> ToolResponse:
    try:
        payload = await client.search(
            params.query,
            object_id=params.object_id,
            search_type=params.search_type,
            include_content=params.include_content,
            limit=params.limit,
            offset=params.offset,
        )
    except KiteworksAPIError as e:
        return make_error_response(
            code=f"KITEWORKS_{e.status}",
            message=str(e),
            details={"body": e.body, "query": params.query},
            fmt=params.response_format,
        )
    rows = payload.get("data") if isinstance(payload, dict) else (payload or [])
    rows = rows or []
    sliced = rows[: params.limit]
    return make_list_response(
        items=[_slim_mixed(r) for r in sliced],
        offset=params.offset,
        limit=params.limit,
        total=len(rows) + params.offset,
        fmt=params.response_format,
        title=f"Search results for {params.query!r}",
        columns=_MIXED_COLS,
    )
