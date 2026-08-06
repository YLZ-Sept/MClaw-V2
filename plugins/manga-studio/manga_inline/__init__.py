"""manga-studio vendored helpers.

These modules used to live under ``mclaw-plugin-sdk.contrib.*`` but were
inlined here in 0.7.0 when the SDK retracted its contrib subpackage. Each
file is a verbatim copy of the avatar-studio ``avatar_studio_inline/`` peer
(which itself was forked from SDK 0.6.0); do not re-import the SDK contrib
path — it no longer exists.

Modules:

- :mod:`manga_inline.vendor_client`     — ``BaseVendorClient`` /
  ``VendorError`` / ``ERROR_KIND_*`` for every vendor adapter (Ark,
  DashScope, OSS).
- :mod:`manga_inline.upload_preview`    — ``add_upload_preview_route`` /
  ``build_preview_url`` for the ``/uploads`` route.
- :mod:`manga_inline.storage_stats`     — ``collect_storage_stats`` /
  ``StorageStats`` for the Settings storage section.
- :mod:`manga_inline.llm_json_parser`   — ``parse_llm_json_object`` for
  parsing brain-generated manga script / storyboard JSON with five-level
  fallback.
- :mod:`manga_inline.parallel_executor` — ``run_parallel`` for bounded
  concurrent panel generation / animate / TTS fan-out.
- :mod:`manga_inline.oss_uploader`      — ``OssUploader`` /
  ``OssNotConfigured`` / ``OssUploadError`` — pushes character reference
  images, panel images and final videos to Aliyun OSS so DashScope and
  ComfyUI can fetch them via signed URL.
"""
