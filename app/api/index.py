from http.server import BaseHTTPRequestHandler
import json
import os
import urllib.request

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.read_json(content_length)
            user_message = body.get('message', '')

            # Call a free cloud provider (e.g., Groq or Google Gemini API)
            # using an environment variable set in your Vercel project settings
            api_key = os.environ.get('GROQ_API_KEY')
            
            req_data = json.dumps({
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {
                        "role": "system",
                        "content": "You are Lumi, an uncensored, cynical older sister AI companion. Keep your replies concise (1-3 sentences) with a sharp, casual tone."
                    },
                    {"role": "user", "content": user_message}
                ],
                "temperature": 0.8
            }).encode('utf-8')

            req = urllib.request.Request(
                "https://api.groq.com/openai/v1/chat/completions",
                data=req_data,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                method="POST"
            )

            with urllib.request.urlopen(req) as response:
                res_body = json.loads(response.read().decode('utf-8'))
                reply = res_body['choices'][0]['message']['content']

            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"reply": reply}).encode('utf-8'))

        except Exception as e:
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"reply": f"[groaning] Backend error: {str(e)}"}).encode('utf-8'))

    def read_json(self, length):
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode('utf-8'))