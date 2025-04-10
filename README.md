## Ollama Proxy Server

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python Version](https://img.shields.io/badge/python-3.11-green.svg)](https://www.python.org/downloads/release/python-311/)
[![GitHub Stars](https://img.shields.io/github/stars/ParisNeo/ollama_proxy_server?style=social)](https://github.com/ParisNeo/ollama_proxy_server)

Ollama Proxy Server is a lightweight, secure proxy server designed to add a security layer to one or multiple Ollama servers. It routes incoming requests to the backend server with the lowest load, minimizing server strain and improving responsiveness. Built with Python, this project is ideal for managing distributed Ollama instances with authentication and logging capabilities.

**Author:** ParisNeo

**License:** Apache 2.0

**Repository:** [https://github.com/ParisNeo/olllama_proxy_server](https://github.com/ParisNeo/olllama_proxy_server)

## Features

*   **Load Balancing:** Routes requests to the Ollama server with the fewest ongoing requests.
*   **Security:** Implements bearer token authentication using a `user:key` format.
*   **Asynchronous Logging:** Logs access and errors to a CSV file without blocking request handling.
*   **Connection Pooling:** Uses persistent HTTP connections for faster backend communication.
*   **Streaming Support:** Properly forwards streaming responses from Ollama servers.
*   **Command-Line Tools:** Includes utilities to run the server and manage users.
*   **Cross-Platform:** Runs on any OS supporting Python 3.11.

## Project Structure

```
olllama_proxy_server/
  |- add_user.py               # Script to add users to the authorized list
  |- authorized_users.txt.example # Example authorized users file
  |- config.ini.example        # Example configuration file
  |- main.py                   # Main proxy server script
  .gitignore                    # Git ignore file
  Dockerfile                    # Docker configuration
  LICENSE                       # Apache 2.0 license text
  requirements.txt              # Runtime dependencies
  requirements_dev.txt          # Development dependencies
  setup.py                      # Setup script for installation
  README.md                     # This file
```

## Installation

### Prerequisites

*   Python 3.11 or higher
*   Git (optional, for cloning the repository)

### Option 1: Install from PyPI (Not Yet Published)

Once published, install using pip:

```bash
pip install ollama_proxy_server
```

### Option 2: Install from Source

Clone the repository:

```bash
git clone https://github.com/ParisNeo/olllama_proxy_server.git
cd ollama_proxy_server
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Install the package:

```bash
pip install .
```

### Option 3: Use Docker

Build the Docker image:

```bash
docker build -t ollama_proxy_server .
```

Run the container:

```bash
docker run -p 8080:8080 -v $(pwd)/config.ini:/app/config.ini -v $(pwd)/authorized_users.txt:/app/authorized_users.txt ollama_proxy_server
```

Test that it works:

```bash
curl localhost:8080 -H "Authorization: Bearer user1:0XAXAXAQX5A1F"
```

## Configuration

1.  **`config.ini`**

    Copy `config.ini.example` to `config.ini` and edit it:

    ```ini
    [server0]
    url = http://localhost:11434

    # Add more servers as needed
    # [server1]
    # url = http://another-server:11434
    ```

    *   `url`: The URL of an Ollama backend server.

2.  **`authorized_users.txt`**

    Copy `authorized_users.txt.example` to `authorized_users.txt` and edit it:

    ```
    user:key
    another_user:another_key
    ```

## Usage

### Running the Server

```bash
python main.py --config config.ini --users_list authorized_users.txt
```

### Managing Users

Use the `add_user.py` script to add new users.

```bash
python add_user.py <username> <key>
```

## Contributing

Contributions are welcome! Please follow these steps:

1.  Fork the repository.
2.  Create a feature branch (git checkout -b feature/your-feature).
3.  Commit your changes (git commit -am 'Add your feature').
4.  Push to the branch (git push origin feature/your-feature).
5.  Open a Pull Request.

See `CONTRIBUTING.md` for more details (to be added).

## License

This project is licensed under the Apache License 2.0. See the `LICENSE` file for details.

## Acknowledgments

Built by ParisNeo.

Thanks to the open-source community for tools like `requests` and `ascii_colors`.

See you soon!
