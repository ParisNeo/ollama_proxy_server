import configparser
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs
from queue import Queue
import requests
import threading
import argparse


def get_config(filename):
    config = configparser.ConfigParser()
    config.read(filename)
    return [(name, {'url': config[name]['url'], 'queue': Queue()}) for name in config.sections()]



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config',default="config.ini", help='Path to the config file') # , required=True
    parser.add_argument('--port', type=int, default=8000, help='Port number for the server')
    args = parser.parse_args()
    servers = get_config(args.config)  

    class RequestHandler(BaseHTTPRequestHandler):

        def _send_response(self, response):
            self.send_response(response.status_code)
            self.send_header('Content-type', response.headers['content-type'])
            self.end_headers()
            self.wfile.write(response.content)

        def do_GET(self):
            self.proxy()

        def do_POST(self):
            self.proxy()

        def proxy(self):
            url = urlparse(self.path)
            path = url.path
            get_params = parse_qs(url.query) or {}

            if self.command == "POST":
                content_length = int(self.headers['Content-Length'])
                post_data = self.rfile.read(content_length)
                post_params = post_data# parse_qs(post_data.decode('utf-8'))
            else:
                post_params = {}


            # Find the server with the lowest number of queue entries.
            min_queued_server = servers[0]
            for server in servers:
                cs = server[1]
                if cs['queue'].qsize() < min_queued_server[1]['queue'].qsize():
                    min_queued_server = server

            # Apply the queuing mechanism only for a specific endpoint.
            if path == '/api/generate':
                que = min_queued_server[1]['queue']
                que.put_nowait(1)
                try:
                    response = requests.request(self.command, min_queued_server[1]['url'] + path, params=get_params, data=post_params)
                    self._send_response(response)
                    self.wfile.write(response.content)
                finally:
                    que.get_nowait()
            else:
                # For other endpoints, just mirror the request.
                response = requests.request(self.command, min_queued_server[1]['url'] + path, params=get_params, data=post_params)
                self._send_response(response)

    class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
        pass


    print('Starting server')
    server = ThreadedHTTPServer(('', args.port), RequestHandler)  # Set the entry port here.
    print(f'Running server on port {args.port}')
    server.serve_forever()

if __name__ == "__main__":
    main()
