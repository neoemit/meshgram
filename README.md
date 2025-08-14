# Meshgram: A Meshtastic ↔ Telegram Bridge

[![Python Version](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/release/python-3120/)
[![License: GPLv3](https://img.shields.io/badge/License-GPLv3-yellow.svg)](https://opensource.org/license/gpl-3-0)

A simple and efficient bridge to forward messages between a Meshtastic mesh network and a Telegram group. This allows seamless communication between users on both platforms.

## Demo

![example](https://github.com/user-attachments/assets/d4002852-596d-4027-b2ff-7d4f72bc3457)

## Key Features

-   **Bidirectional Forwarding:** Messages are relayed from Meshtastic to Telegram and vice versa.
-   **Node Filtering:** An allowlist ensures only messages from specific Meshtastic nodes are forwarded.
-   **Secure Configuration:** All sensitive information, like API tokens, is managed through environment variables.
-   **Easy to Deploy:** Simple setup with minimal dependencies.

## Requirements

-   Python 3.12.x
-   A Meshtastic device connected via serial or TCP.
-   A Telegram Bot Token and the ID of the target group chat.

## Installation

1.  **Clone the Repository**

    ```bash
    git clone https://github.com/neoemit/meshgram.git
    cd meshgram
    ```

2.  **Create and Activate a Virtual Environment**

    ```bash
    python3 -m venv .venv
    source .venv/bin/activate
    ```

3.  **Install Dependencies**

    ```bash
    pip install -r requirements.txt
    ```

## Configuration

1.  **Environment Variables**

    Create a `.env` file in the project's root directory with the following content:

    ```ini
    # Your Telegram Bot API Token
    TELEGRAM_BOT_TOKEN=123456789:ABC-YourBotTokenHere

    # The ID of your Telegram Group (must start with a '-')
    TELEGRAM_GROUP_ID=-987654321

    # Optional: Specify the Meshtastic device path (e.g., /dev/ttyUSB0)
    # If commented out, the script will attempt to auto-detect the device.
    # MESH_DEVICE=/dev/ttyUSB0
    ```

2.  **Node Allowlist**

    To control which Meshtastic nodes can communicate with the Telegram group, edit the `ALLOWED_NODES` dictionary in `main.py`. Use a friendly name for the key and the node's ID as the value.

    ```python
    # main.py

    ALLOWED_NODES = {
        "NodeName1": "!nodeId1",
        "NodeName2": "!nodeId2"
    }
    ```

## Usage

Once the configuration is complete, run the bridge from your terminal:

```bash
python main.py
```

> **Note:** This script has been primarily tested on macOS and Linux environments.

## How It Works

The script initializes connections to both the Meshtastic device and the Telegram API. It then enters a loop, listening for incoming messages from either source.

-   **Meshtastic to Telegram:** When a message is received from an allowed Meshtastic node, it is formatted with the nickname label and forwarded to the specified Telegram group.
-   **Telegram to Meshtastic:** When a message is sent to the Telegram group, it is relayed to whitelisted nodes on the Meshtastic network based on nickname handle syntax.

---

Contributions are welcome! Please feel free to submit a pull request or open an issue.
