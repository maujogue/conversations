import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock

ROOT = Path(__file__).parent
lock = Lock()

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *args):
        pass
    def do_GET(self):
        data = json.dumps({'object': 'list', 'data': [{'id': 'review-champion'}, {'id': 'review-challenger'}]}).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        with lock:
            with (ROOT / 'logs/mock-requests.jsonl').open('a') as output:
                output.write(json.dumps({'at': time.time(), 'request': body}) + '\n')
        model = body['model']
        prompt = str(body.get('messages', [])[-1].get('content', ''))
        if '[FAIL_CHALLENGER]' in prompt and model == 'review-challenger':
            error = json.dumps({'error': {'message': 'Intentional QA provider failure', 'type': 'server_error'}}).encode()
            self.send_response(503); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(error))); self.end_headers(); self.wfile.write(error); return
        if '[FAIL_CHAMPION]' in prompt and model == 'review-champion':
            error = json.dumps({'error': {'message': 'Intentional QA champion failure', 'type': 'server_error'}}).encode()
            self.send_response(503); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(error))); self.end_headers(); self.wfile.write(error); return
        text = ('Bonjour,\n\nMerci pour votre message. Voici une proposition structurée :\n\n'
                '**Objet : Suivi de notre réunion**\n\n'
                'Nous vous confirmons la réception de votre demande. Notre équipe prépare les éléments nécessaires et reviendra vers vous vendredi.\n\n'
                '**Actions à réaliser**\n\n1. Vérifier les informations du dossier.\n2. Préparer une synthèse pour validation.\n3. Transmettre la réponse au service concerné.\n\n'
                'Cordialement,\nLe service administratif')
        if model == 'review-challenger':
            text = ('Bonjour,\n\nNous avons bien reçu votre demande. Vous recevrez notre réponse vendredi.\n\n'
                    'D’ici là, nous allons :\n- vérifier votre dossier ;\n- préparer la synthèse ;\n- faire valider la réponse.\n\nCordialement,\nLe service administratif')
        if '[LONG]' in prompt:
            text += '\n\n' + '\n\n'.join(f'### Point {i}\nVoici les éléments de suivi à vérifier avant la validation du document. ' * 2 for i in range(1, 14))
        if '[IDENTITY]' in prompt:
            tools = [x for x in body.get('messages', []) if x.get('role') == 'tool']
            if tools:
                text = 'Documentation interne : ' + tools[-1]['content']
            else:
                self.sse_start()
                self.chunk(model, {'tool_calls': [{'index': 0, 'id': 'call_identity', 'type': 'function', 'function': {'name': 'self_documentation', 'arguments': '{}'}}]}, None)
                self.chunk(model, {}, 'tool_calls')
                self.wfile.write(b'data: [DONE]\n\n'); self.wfile.flush(); return
        if not body.get('stream'):
            data = json.dumps({'id': 'qa', 'object': 'chat.completion', 'created': int(time.time()), 'model': model, 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 200, 'completion_tokens': 80, 'total_tokens': 280}}).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data); return
        self.sse_start()
        delay = 0.1 if '[SLOW]' in prompt else 0.012
        try:
            for i in range(0, len(text), 20):
                self.chunk(model, {'content': text[i:i+20]}, None)
                time.sleep(delay)
            self.chunk(model, {}, 'stop', {'prompt_tokens': 200, 'completion_tokens': len(text)//4, 'total_tokens': 200+len(text)//4})
            self.wfile.write(b'data: [DONE]\n\n'); self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
    def sse_start(self):
        self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.send_header('Connection', 'close'); self.end_headers(); self.close_connection = True
    def chunk(self, model, delta, finish, usage=None):
        data = {'id': 'qa-stream', 'created': int(time.time()), 'object': 'chat.completion.chunk', 'model': model, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
        if usage: data['usage'] = usage
        self.wfile.write(('data: ' + json.dumps(data) + '\n\n').encode()); self.wfile.flush()

ThreadingHTTPServer(('127.0.0.1', 28900), Handler).serve_forever()
