
# Ollama Proxy Server

Ollama Proxy Server is a lightweight reverse proxy server designed for load balancing and rate limiting. It is built for efficient request management and includes features like user authentication, server queuing, and Docker/Podman support. This project is licensed under the [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0).

---

## **Overview**
This repository is a fork of [ParisNeo/ollama_proxy_server](https://github.com/ParisNeo/ollama_proxy_server). The original project has been extended with:
- Improved documentation.
- Enhanced support for containerized deployment using Docker and Podman.
- Additional usage examples and configuration details.

---

## **Prerequisites**
- Python (>=3.8)
- Docker or Podman (optional for containerized deployment)

---

## **Installation**

### **1. Install via pip**
1. Clone this repository:
   ```bash
   git clone https://github.com/minhlt82/ollama_proxy_server.git
   ```
2. Navigate to the cloned directory:
   ```bash
   cd ollama_proxy_server
   ```
3. Install the package:
   ```bash
   pip install -e .
   ```

### **2. Using Docker**
1. Clone the repository:
   ```bash
   git clone https://github.com/minhlt82/ollama_proxy_server.git
   cd ollama_proxy_server
   ```
2. Build the Docker image:
   ```bash
   docker build -t ollama_proxy_server:latest .
   ```
3. Run the container:
   ```bash
   docker run -d --name ollama-proxy-server -p 8080:8080 ollama_proxy_server:latest
   ```

### **3. Using Podman**
1. Build the image:
   ```bash
   podman build -t ollama_proxy_server:latest .
   ```
2. Run the container:
   ```bash
   podman run -d --name ollama-proxy-server -p 8080:8080 ollama_proxy_server:latest
   ```

---

## **Configuration**

### **Servers Configuration (`config.ini`)**
Create a `config.ini` file in the same directory as the script. Example:
```ini
[DefaultServer]
url = http://localhost:11434
queue_size = 5

[SecondaryServer]
url = http://localhost:3002
queue_size = 3
```

### **Authorized Users (`authorized_users.txt`)**
Create a `authorized_users.txt` file listing `user:key` pairs:
```text
user1:key1
user2:key2
```

To generate a user/key pair, use:
```bash
ollama_proxy_add_user --users_list authorized_users.txt
```

---

## **Usage**

### **Start the Server**
Run the server:
```bash
python3 ollama_proxy_server/main.py --config config.ini --users_list authorized_users.txt --port 8080
```

---

## **Send Requests**
The server supports API key authentication through either the `Authorization` header or the `apiKey` field in the payload.

### **Using Authorization Header**
```bash
curl -X POST -H "Authorization: Bearer user1:key1"      http://localhost:8080/api/generate      --data '{"model":"llama3.2:3b","prompt":"Once upon a time...","stream":false}'
```

### **Using `apiKey` in the Payload**
If you prefer, you can include the API key directly in the payload using the `apiKey` field:
```bash
curl -X POST      http://localhost:8080/api/generate      --data '{"model":"llama3.2:3b","prompt":"Once upon a time...","stream":false,"apiKey":"user1:key1"}'
```

Replace:
- `user1:key1` with a valid username and API key from your `authorized_users.txt`.
- `http://localhost:8080` with the correct server URL and port.
- Payload values (e.g., `model`, `prompt`) with the actual parameters for your request.

---

## **License**
This project is licensed under the [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0).

## **Acknowledgments**
This project is a fork of [ParisNeo/ollama_proxy_server](https://github.com/ParisNeo/ollama_proxy_server). Credit to ParisNeo for the original implementation. This fork includes additional updates for improved deployment and configuration.
