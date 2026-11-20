"""Helpers for handing private files to an authorized client."""
from django.http import FileResponse, HttpResponseRedirect


def _is_s3(storage):
    return hasattr(storage, "bucket_name")


def serve_file(field, filename, inline=False):
    """Respond with a private stored file.

    S3/R2: redirect to a presigned URL (expires per settings, 5 minutes by default).
    Local filesystem: stream the file through Django.
    """
    disposition = "inline" if inline else "attachment"
    storage = field.storage
    if _is_s3(storage):
        url = storage.url(
            field.name,
            parameters={
                "ResponseContentDisposition": f'{disposition}; filename="{filename}"',
                "ResponseContentType": "application/pdf",
            },
        )
        return HttpResponseRedirect(url)
    resp = FileResponse(field.open("rb"), as_attachment=not inline, filename=filename, content_type="application/pdf")
    resp["Cache-Control"] = "private, no-store"
    return resp


def file_exists(field):
    try:
        return bool(field) and field.storage.exists(field.name)
    except Exception:
        return False
