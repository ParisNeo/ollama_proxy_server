"""
project: ollama_proxy_server
file: main.py
author: ParisNeo
description: This is a proxy server that adds a security layer to one or multiple ollama servers and routes the requests to the right server in order to minimize the charge of the server.
"""

import configparser
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs
from queue import Queue
import requests
import threading
import argparse
import base64
from ascii_colors import ASCIIColors

def get_config(filename):
    config = configparser.ConfigParser()
    config.read(filename)
    return [(name, {'url': config[name]['url'], 'queue': Queue()}) for name in config.sections()]

# Read the authorized users and their keys from a file
def get_authorized_users(filename):
    with open(filename, 'r') as f:
        lines = f.readlines()
    authorized_users = {}
    for line in lines:
        user, key = line.strip().split(':')
        authorized_users[user] = key
    return authorized_users



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default="config.ini", help='Path to the authorized users list')
    parser.add_argument('--log_path', default="access_log.txt", help='Path to the access log file')
    parser.add_argument('--users_list', default="authorized_users.txt", help='Path to the config file')
    parser.add_argument('--port', type=int, default=8000, help='Port number for the server')
    args = parser.parse_args()
    servers = get_config(args.config)  
    authorized_users = get_authorized_users(args.users_list)

    class RequestHandler(BaseHTTPRequestHandler):
        def add_access_log_entry(self, user, ip_address, access, server):
            log_file_path = Path(sys.argv[1])
        
            if not log_file_path.exists():
                with open(log_file_path, mode='w', newline='') as csvfile:
                    fieldnames = ['time_stamp', 'user_name', 'ip_address','access','server']
                    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                    writer.writeheader()
        
            with open(log_file_path, mode='a', newline='') as csvfile:
                fieldnames = ['time_stamp', 'user_name', 'ip_address']
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                row = {'time_stamp': str(datetime.datetime.now()), 'user_name': user, 'ip_address': ip_address, 'access': access, 'server': server}
                writer.writerow(row)
                
        def _send_response(self, response):
            self.send_response(response.status_code)
            self.send_header('Content-type', response.headers['content-type'])
            self.end_headers()
            self.wfile.write(response.content)

        def do_GET(self):
            self.log_request()
            self.proxy()

        def do_POST(self):
            self.log_request()
            self.proxy()

        def _validate_user_and_key(self):
            # Extract the bearer token from the headers
            auth_header = self.headers.get('Authorization')
            if not auth_header or not auth_header.startswith('Bearer '):
                return False
            token = auth_header.split(' ')[1]
            user, key = token.split(':')
            
            # Check if the user and key are in the list of authorized users
            if authorized_users.get(user) == key:
                self.user = user
                return True
            else:
                self.user = "unknown"
                return False
        
        def proxy(self):
            if not self._validate_user_and_key():
                ASCIIColors.red(f'User is not authorized')
                client_ip, client_port = self.client_address
                self.add_access_log_entry(user="unknown", ip_address=client_ip, "Denied", "None")
                self.send_response(403)
                self.end_headers()
                return            
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
                self.add_access_log_entry(user=self.user, ip_address=client_ip, "Authorized", min_queued_server[0])
                que.put_nowait(1)
                try:
                    response = requests.request(self.command, min_queued_server[1]['url'] + path, params=get_params, data=post_params)
                    self._send_response(response)
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
