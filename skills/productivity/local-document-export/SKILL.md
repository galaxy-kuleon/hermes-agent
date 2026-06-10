---
name: local-document-export
description: Export local DOCX/PDF download links.
version: 1.0.0
author: admin and Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [documents, export, docx, pdf, openwebui, productivity]
    category: productivity
    related_skills: [ocr-and-documents]
---

# Local Document Export Skill

Use this skill when a user wants conversation content, a draft, or revised
uploaded-file text exported as Word or PDF. It produces local downloadable
artifacts through Hermes; it does not use Google Workspace, Google Docs, Google
Drive, Microsoft cloud APIs, or any hosted document service.

For uploaded files, this creates a new clean document from the extracted and
edited text. It is not a layout-preserving edit, track-changes workflow, or
faithful reconstruction of the original binary file.

## When to Use

- The user asks for Word, DOCX, PDF, downloadable memo, formal letter, minutes,
  recap, quote, report, or working paper output.
- The user says to convert the latest answer, conversation, draft, or revised
  content into a document.
- The user uploads a file, asks for content edits, then asks for Word/PDF.
- The OpenWebUI conversation needs browser-clickable markdown links.

Do not use this skill for high-fidelity PDF-to-DOCX reconstruction, final legal
advice, cloud sharing, or Google Workspace workflows.

## Prerequisites

- The native `local_document_export` tool is available.
- Hermes has trusted platform user/chat context from OpenWebUI or another
  gateway.
- `LOCAL_EXPORT_ARTIFACT_SIGNING_KEY` is configured so download URLs are
  signed and expiring.
- `LOCAL_EXPORT_PUBLIC_BASE_URL` points to a browser-reachable Hermes API
  server URL. `http://localhost:8642` is only a local desktop demo default.

## How to Run

Prepare the final Markdown content first. Apply requested edits before calling
`local_document_export`; do not export a rough intermediate answer unless the
user explicitly asks for it.

Call `local_document_export` with:

- `content_markdown`: the finished Markdown document body.
- `formats`: `["docx"]`, `["pdf"]`, or `["docx", "pdf"]`.
- `filename_stem`: a short human-readable base name when useful.
- `title`: optional document title.

## Quick Reference

Use DOCX when the user asks for Word, editable output, `.docx`, or a draft they
can revise. Use PDF when the user asks for printable, shareable, or fixed-format
output. Use both when the user says Word/PDF, DOCX/PDF, or "downloadable files"
without choosing one.

OpenWebUI responses should contain concise confirmation text followed by
markdown links from the tool result. Do not return `MEDIA:` tags for OpenWebUI.
Paste the exact `markdown` string returned by `local_document_export` verbatim
in the final OpenWebUI answer. Do not rewrite URLs, do not HTML-escape `&` as
`&amp;`, and do not insert spaces inside markdown link targets such as
`]( URL)`.
When exact legal names or address strings are provided, preserve ASCII
apostrophes (`'`, U+0027) in `content_markdown`; do not replace them with curly
apostrophes (`’`, U+2019).

## Procedure

1. Identify the source content: latest answer, conversation summary, pasted
   text, uploaded-file Markdown, or a draft you just revised.
2. Make the requested content edits first.
3. Build a clean Markdown document with headings, paragraphs, lists, and tables
   as appropriate.
4. Call `local_document_export` with the requested formats.
5. Reply with the tool's markdown links and expiry context. Use the exact
   returned markdown text verbatim so signed URLs keep their original query
   string.
6. If the export came from an uploaded file, state that the result is a new
   clean document, not a layout-preserving edit of the original file.

## Pitfalls

- Never route this workflow through Google Workspace or cloud document APIs.
- Do not ask the model to provide `user_id` or `chat_id`; the tool must use
  trusted gateway context.
- Do not promise original formatting, embedded comments, tracked changes, or
  exact page layout for uploaded-file edits.
- Do not paste raw local filesystem paths into the user response. Use the
  signed markdown links returned by the tool.
- Do not claim success if the tool returns an error or missing link.

## Verification

- The `local_document_export` result has `success: true`.
- Each requested format appears in the returned artifacts.
- The response contains markdown links for the user to click in OpenWebUI.
- The document is described as a working draft when used for legal or customer
  workflows.
