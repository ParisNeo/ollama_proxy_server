"""
project: ollama_proxy_server
file: main.py
author: ParisNeo (Saifeddine ALOUI)
description: A proxy server adding a security layer to one or multiple Ollama servers, routing requests to minimize server load.
license: Apache 2.0
repository: https://github.com/ParisNeo/ollama_proxy_server
"""

import configparser
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs
from queue import Queue
import requests
import argparse
from ascii_colors import ASCIIColors
from pathlib import Path
import csv
import datetime
import threading
import shutil

def get_config(filename):
    config = configparser.ConfigParser()
    config.read(filename)
    return [(name, {
        'url': config[name]['url'],
        'session': requests.Session(),
        'ongoing_requests': 0,
        'lock': threading.Lock()
    }) for name in config.sections()]

def get_authorized_users(filename):
    with open(filename, 'r') as f:
        lines = f.readlines()
    authorized_users = {}
    for line in lines:
        if line.strip() == "":
            continue
        try:
            user, key = line.strip().split(':')
            authorized_users[user] = key
        except:
            ASCIIColors.red(f"User entry broken: {line.strip()}")
    return authorized_users

def log_writer(log_queue, log_file_path):
    with open(log_file_path, mode='a', newline='') as csvfile:
        fieldnames = ['time_stamp', 'event', 'user_name', 'ip_address', 'access', 'server', 'nb_queued_requests_on_server', 'error']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        if csvfile.tell() == 0:
            writer.writeheader()
        while True:
            log_entry = log_queue.get()
            if log_entry is None:  # Signal to exit
                break
            writer.writerow(log_entry)
            csvfile.flush()

def main():
    parser = argparse.ArgumentParser(description="Ollama Proxy Server by ParisNeo")
    parser.add_argument('--config', default="config.ini", help='Path to the config file')
    parser.add_argument('--log_path', default="access_log.txt", help='Path to the access log file')
    parser.add_argument('--users_list', default="authorized_users.txt", help='Path to the authorized users list')
    parser.add_argument('--port', type=int, default=8000, help='Port number for the server')
    parser.add_argument('-d', '--deactivate_security', action='store_true', help='Deactivates security')
    args = parser.parse_args()
    servers = get_config(args.config)
    authorized_users = get_authorized_users(args.users_list)
    deactivate_security = args.deactivate_security
    ASCIIColors.red("Ollama Proxy Server")
    ASCIIColors.red("Author: ParisNeo (Saifeddine ALOUI)")
    ASCIIColors.red("License: Apache 2.0")
    ASCIIColors.red("Repository: https://github.com/ParisNeo/ollama_proxy_server")

    global log_queue
    log_queue = Queue()
    log_file_path = Path(args.log_path)
    if not log_file_path.exists() or log_file_path.stat().st_size == 0:
        with open(log_file_path, mode='w', newline='') as csvfile:
            fieldnames = ['time_stamp', 'event', 'user_name', 'ip_address', 'access', 'server', 'nb_queued_requests_on_server', 'error']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
    log_writer_thread = threading.Thread(target=log_writer, args=(log_queue, log_file_path))
    log_writer_thread.daemon = True
    log_writer_thread.start()

    class RequestHandler(BaseHTTPRequestHandler):
        def add_access_log_entry(self, event, user, ip_address, access, server, nb_queued_requests_on_server, error=""):
            log_entry = {
                'time_stamp': str(datetime.datetime.now()),
                'event': event,
                'user_name': user,
                'ip_address': ip_address,
                'access': access,
                'server': server,
                'nb_queued_requests_on_server': nb_queued_requests_on_server,
                'error': error
            }
            log_queue.put(log_entry)

        def _send_response(self, response):
            self.send_response(response.status_code)
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.end_headers()
            try:
                shutil.copyfileobj(response.raw, self.wfile)
                self.wfile.flush()
            except BrokenPipeError:
                pass

        def do_HEAD(self):
            self.log_request()
            self.proxy()

        def do_GET(self):
            self.log_request()
            self.proxy()

        def do_POST(self):
            self.log_request()
            self.proxy()

        def _validate_user_and_key(self):
            try:
                auth_header = self.headers.get('Authorization')
                if not auth_header or not auth_header.startswith('Bearer '):
                    return False
                token = auth_header.split(' ')[1]
                user, key = token.split(':')
                if authorized_users.get(user) == key:
                    self.user = user
                    return True
                else:
                    self.user = "unknown"
                return False
            except:
                return False

        def proxy(self):
            self.user = "unknown"
            if not deactivate_security and not self._validate_user_and_key():
                ASCIIColors.red('User is not authorized')
                client_ip, _ = self.client_address
                auth_header = self.headers.get('Authorization')
                if not auth_header or not auth_header.startswith('Bearer '):
                    self.add_access_log_entry(event='rejected', user="unknown", ip_address=client_ip, access="Denied", server="None", nb_queued_requests_on_server=-1, error="Authentication failed")
                else:
                    token = auth_header.split(' ')[1]
                    self.add_access_log_entry(event='rejected', user=token, ip_address=client_ip, access="Denied", server="None", nb_queued_requests_on_server=-1, error="Authentication failed")
                self.send_response(403)
                self.end_headers()
                return

            url = urlparse(self.path)
            path = url.path
            get_params = parse_qs(url.query) or {}
            post_params = {}
            if self.command == "POST":
                content_length = int(self.headers['Content-Length'])
                post_params = self.rfile.read(content_length)

            min_queued_server = min(servers, key=lambda s: s[1]['ongoing_requests'])

            if path in ['/api/generate', '/api/chat', '/v1/chat/completions']:
                with min_queued_server[1]['lock']:
                    min_queued_server[1]['ongoing_requests'] += 1
                client_ip, _ = self.client_address
                self.add_access_log_entry(event="gen_request", user=self.user, ip_address=client_ip, access="Authorized", server=min_queued_server[0], nb_queued_requests_on_server=min_queued_server[1]['ongoing_requests'])
                try:
                    post_data_dict = json.loads(post_params.decode('utf-8')) if isinstance(post_params, bytes) else {}
                    response = min_queued_server[1]['session'].request(
                        self.command,
                        min_queued_server[1]['url'] + path,
                        params=get_params,
                        data=post_params,
                        stream=post_data_dict.get("stream", False)
                    )
                    self._send_response(response)
                except Exception as ex:
                    self.add_access_log_entry(event="gen_error", user=self.user, ip_address=client_ip, access="Authorized", server=min_queued_server[0], nb_queued_requests_on_server=min_queued_server[1]['ongoing_requests'], error=str(ex))
                finally:
                    with min_queued_server[1]['lock']:
                        min_queued_server[1]['ongoing_requests'] -= 1
                    self.add_access_log_entry(event="gen_done", user=self.user, ip_address=client_ip, access="Authorized", server=min_queued_server[0], nb_queued_requests_on_server=min_queued_server[1]['ongoing_requests'])
            else:
                response = min_queued_server[1]['session'].request(
                    self.command,
                    min_queued_server[1]['url'] + path,
                    params=get_params,
                    data=post_params
                )
                self._send_response(response)

    class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
        pass

    print('Starting server')
    server = ThreadedHTTPServer(('', args.port), RequestHandler)
    print(f'Running server on port {args.port}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log_queue.put(None)  # Signal log_writer to exit
        server.server_close()

if __name__ == "__main__":
    main()
