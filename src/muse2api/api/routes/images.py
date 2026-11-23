from __future__ import annotations

import base64
import mimetypes
import time
from dataclasses import replace
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError
from starlette.datastructures import FormData, UploadFile

from ...core.media import load_image_ref
from ...core.models import resolve_model
from ...drivers.base import ImageRequest, InputImage, MediaResult, ProgressCallback
from ...errors import InvalidRequest, NotFound
from ...services.container import Services
from ...services.tasks import Task
from ..deps import get_services, public_base, require_api_key
from ..schemas import ImageGenerationRequest

router = APIRouter(tags=["images"], dependencies=[Depends(require_api_key)])

# Reference images attached to one muse.ai message.
MAX_REFERENCE_IMAGES = 4

# Asking muse.ai for a "transparent background" makes it reply with text only, so
# ask for an easy-to-cut plain background and remove it locally instead.
_CUTOUT_HINT = "Isolated subject on a plain, uncluttered studio background."


def _image_item(r: MediaResult, fmt: str, svc: Services, base: str) -> dict[str, Any]:
    item: dict[str, Any] = {"revised_prompt": r.revised_prompt}
    if fmt == "b64_json":
        item["b64_json"] = base64.b64encode(r.data).decode()
    else:
        name = svc.media.save(r.data, r.mime, prefix="img")
        item["url"] = f"{base}/v1/media/{name}"
    return item


def _task_view(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "object": "image.task",
        "status": task.status.value,
        "progress": task.progress,
        "created_at": int(task.created_at),
        "model": task.request.get("model"),
        "result": task.result,
        "error": task.error,
    }


async def _generate(body: ImageGenerationRequest, refs: list[InputImage],
                    request: Request, svc: Services) -> dict[str, Any]:
    request.state.model = body.model or resolve_model(body.model, "image").id
    if len(refs) > MAX_REFERENCE_IMAGES:
        message = f"at most {MAX_REFERENCE_IMAGES} reference images are supported"
        raise InvalidRequest(message)
    if body.async_:
        return _submit(body, refs, request, svc)
    return await _run(body, refs, public_base(request), svc)


def _submit(body: ImageGenerationRequest, refs: list[InputImage], request: Request,
            svc: Services) -> dict[str, Any]:
    """Run the generation as a background task; results are stored as media URLs."""
    spec = resolve_model(body.model, "image")
    body = body.model_copy(update={"response_format": "url"})  # keep base64 out of tasks.json
    base = public_base(request)

    async def runner(_progress: ProgressCallback) -> dict[str, Any]:
        return await _run(body, refs, base, svc)

    meta: dict[str, Any] = {"model": spec.id, "prompt": body.prompt, "size": body.size,
                             "n": body.n, "references": len(refs)}
    task = svc.tasks.submit("image", meta, runner)
    request.state.task_id = task.id
    return _task_view(task)


async def _run(body: ImageGenerationRequest, refs: list[InputImage], base: str,
               svc: Services) -> dict[str, Any]:
    spec = resolve_model(body.model, "image")
    transparent = body.background == "transparent"
    prompt = f"{body.prompt.rstrip('. ')}. {_CUTOUT_HINT}" if transparent else body.prompt
    req = ImageRequest(prompt=prompt, model=spec.id, size=body.size, n=body.n,
                       reference_images=refs, timeout=svc.settings.image_timeout)
    results = await svc.gateway.generate_image(req)
    if transparent:
        # After the gateway call, so the account is not held during matting.
        results = [replace(r, data=await svc.matting.remove(r.data), mime="image/png")
                   for r in results]
    return {"created": int(time.time()),
            "data": [_image_item(r, body.response_format, svc, base) for r in results]}


@router.post("/v1/images/generations")
async def generate_images(body: ImageGenerationRequest, request: Request,
                          svc: Annotated[Services, Depends(get_services)]) -> dict[str, Any]:
    if isinstance(body.image, list):
        refs = body.image
    elif body.image:
        refs = [body.image]
    else:
        refs = []
    loaded = [InputImage(*(await load_image_ref(ref))) for ref in refs]
    return await _generate(body, loaded, request, svc)


def _parse_edit_form(form: FormData) -> tuple[list[UploadFile], dict[str, str]]:
    """Split an edit multipart form into image files and string fields."""
    if form.get("mask") is not None:
        message = "mask is not supported; muse.ai cannot inpaint a region"
        raise InvalidRequest(message)
    files = [f for key in ("image", "image[]") for f in cast(list[Any], form.getlist(key))
             if isinstance(f, UploadFile)]
    if not files:
        message = "at least one image file is required (field 'image' or 'image[]')"
        raise InvalidRequest(message)
    fields = {
        k: v
        for k, v in form.items()
        if isinstance(v, str) and k not in ("image", "image[]")
    }
    return files, fields


async def _reference_images(files: list[UploadFile]) -> list[InputImage]:
    """Read uploaded files into InputImage references, guessing mime when missing."""
    refs: list[InputImage] = []
    for f in files:
        mime = f.content_type or ""
        if not mime.startswith("image/"):
            mime = mimetypes.guess_type(f.filename or "")[0] or "image/png"
        refs.append(InputImage(data=await f.read(), mime=mime))
    return refs


@router.post("/v1/images/edits")
async def edit_images(request: Request, svc: Annotated[Services, Depends(get_services)]) -> dict[str, Any]:
    """OpenAI-style multipart edit: ``image`` / ``image[]`` files plus a prompt.

    muse.ai has no inpainting, so the images act as references for a new image
    and ``mask`` is rejected rather than silently ignored.
    """
    form = await request.form()
    files, fields = _parse_edit_form(form)
    try:
        body = ImageGenerationRequest(**cast(Any, fields))
    except ValidationError as exc:
        raise InvalidRequest(exc.errors()[0].get("msg", "invalid request")) from exc
    refs = await _reference_images(files)
    return await _generate(body, refs, request, svc)


@router.get("/v1/images/generations/{task_id}")
async def get_image_task(task_id: str, request: Request,
                         svc: Annotated[Services, Depends(get_services)]) -> dict[str, Any]:
    task = svc.tasks.get(task_id)
    if task is None or task.kind != "image":
        message = f"image task '{task_id}' not found"
        raise NotFound(message)
    request.state.model = task.request.get("model")
    return _task_view(task)
