# Ollama Proxy Server

Ollama Proxy Server is a lightweight reverse proxy server designed for load balancing and rate limiting. It is licensed under the Apache 2.0 license and can be installed using pip. This README covers setting up, installing, and using the Ollama Proxy Server.

## Prerequisites
Make sure you have Python (>=3.8) and Apache installed on your system before proceeding.

## Installation
1. Clone or download the `ollama_proxy_server` repository from GitHub: https://github.com/ParisNeo/ollama_proxy_server
2. Navigate to the cloned directory in the terminal and run `pip install -e .`

## Installation using Dockerfile
1. Clone this repository as described above.
2. Build your Container-Image with the Dockerfile provided by this repository

### Podman
`cd ollama_proxy_server`  
`podman build -t ollama_proxy_server:latest .`

### Docker
`cd ollama_proxy_server`  
`docker build -t ollama_proxy_server:latest .`

## Configuration

### Servers configuration (config.ini)
Create a file named `config.ini` in the same directory as your script, containing server configurations:
```makefile
[DefaultServer]
url = http://localhost:11434
queue_size = 5

[SecondaryServer]
url = http://localhost:3002
queue_size = 3

# Add as many servers as needed, in the same format as [DefaultServer] and [SecondaryServer].
```
Replace `http://localhost:11434/` with the URL and port of the first server. The `queue_size` value indicates the maximum number of requests that can be queued at a given time for this server.

### Authorized users (authorized_users.txt)
Create a file named `authorized_users.txt` in the same directory as your script, containing a list of user:key pairs, separated by commas and each on a new line:
```text
user1,key1
user2,key2
```
Replace `user1`, `key1`, `user2`, and `key2` with the desired username and API key for each user.
You can also use the `ollama_proxy_add_user` utility to add user and generate a key automatically: 
```makefile
ollama_proxy_add_user --users_list [path to the authorized `authorized_users.txt` file]
```

## Usage
### Starting the server
Start the Ollama Proxy Server by running the following command in your terminal:
```bash
ollama_proxy_server --config [configuration file path] --users_list [users list file path] --port [port number to access the proxy]
```
The server will listen on port 808x, with x being the number of available ports starting from 0 (e.g., 8080, 8081, etc.). The first available port will be automatically selected if no other instance is running.

### Client requests
To send a request to the server, use the following command:
```bash
curl -X <METHOD> -H "Authorization: Bearer <USER_KEY>" http://localhost:<PORT>/<PATH> [--data <POST_DATA>]
```
Replace `<METHOD>` with the HTTP method (GET or POST), `<USER_KEY>` with a valid user:key pair from your `authorized_users.txt`, `<PORT>` with the port number of your running Ollama Proxy Server, and `<PATH>` with the target endpoint URL (e.g., "/api/generate"). If you are making a POST request, include the `--data <POST_DATA>` option to send data in the body.

For example:
```bash
curl -X POST -H "Authorization: Bearer user1:key1" http://localhost:8080/api/generate --data '{"data": "Hello, World!"}'
``` 
### Starting the server using the created Container-Image
To start the proxy in background with the above created image, you can use either   
1) docker: `docker run -d --name ollama-proxy-server -p 8080:8080 ollama_proxy_server:latest`
2) podman: `podman run -d --name ollama-proxy-server -p 8080:8080 ollama_proxy_server:latest`
