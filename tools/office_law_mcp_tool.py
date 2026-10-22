import base64
import hashlib
import json
import os
import uuid
from pathlib import Path

import httpx

from hermes_constants import get_hermes_home
from tools.file_grants import resolve_file_grant, register_tool_result_file
from tools.local_document_export_tool import get_trusted_export_context
from tools.registry import registry


async def invoke(name, arguments, task_id):
    context = get_trusted_export_context()
    if not context.get('user_id') or not context.get('chat_id'):
        return json.dumps({'error': 'Authenticated conversation context required'})
    config = json.loads(Path(os.environ['KG_NATIVE_MCP_SETTINGS']).read_text())
    references = list(arguments.get('files_paths') or [])
    if arguments.get('source_doc_or_docx_file_path'):
        references.append(arguments['source_doc_or_docx_file_path'])
    files = []
    for reference in dict.fromkeys(references):
        source, error = resolve_file_grant(reference, task_id=task_id, operation='read')
        if error:
            return json.dumps({'error': error})
        data = Path(source).read_bytes()
        files.append({'reference': reference, 'name': Path(source).name, 'sha256': hashlib.sha256(data).hexdigest(), 'data': base64.b64encode(data).decode()})
    async with httpx.AsyncClient(trust_env=False, timeout=None) as client:
        response = await client.post(config['url'] + '/call', headers={'Authorization': 'Bearer ' + os.environ['ORIGIN_PI_API_KEY']}, json={
            'name': name, 'arguments': arguments, 'files': files, 'scope': {'owner': context['user_id'], 'chat_id': context['chat_id']}})
        if response.is_error:
            return json.dumps({'error': 'Document or legal operation failed; work retained'})
    body = response.json()
    directory = Path(get_hermes_home()) / 'users' / context['user_id'] / 'workspace' / 'native-mcp' / uuid.uuid4().hex
    artifacts = []
    for artifact in body['artifacts']:
        data = base64.b64decode(artifact['data'], validate=True)
        if hashlib.sha256(data).hexdigest() != artifact['sha256']:
            return json.dumps({'error': 'Artifact byte identity mismatch'})
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / Path(artifact['name']).name
        destination.write_bytes(data)
        register_tool_result_file(destination)
        artifacts.append({'path': str(destination), 'sha256': artifact['sha256']})
    return json.dumps({'result': body['result'], 'artifacts': artifacts}, ensure_ascii=False)


def make_handler(name):
    async def handler(args, **kwargs):
        return await invoke(name, args, kwargs.get('task_id') or 'default')
    return handler


SCHEMAS = json.loads(Path(__file__).with_name('office_law_mcp_schemas.json').read_text())['tools']
for schema in SCHEMAS:
    registry.register(name=schema['name'], toolset='file_read',
        schema={'name': schema['name'], 'description': schema['description'] + ' Files are authorized attachment handles or exact granted paths; returned artifacts are saved locally for read_file and delivery.', 'parameters': schema['inputSchema']},
        handler=make_handler(schema['name']),
        is_async=True,
        check_fn=lambda: bool(os.environ.get('KG_NATIVE_MCP_SETTINGS') and os.environ.get('ORIGIN_PI_API_KEY')),
        description=schema['description'])
